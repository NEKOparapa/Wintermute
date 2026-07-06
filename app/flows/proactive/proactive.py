from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

from ...config.config import Settings
from ...infrastructure.llm.llm import LLMResponse, OpenAICompatibleLLM, ToolCall
from ...infrastructure.prompt.l1_prompt import build_l1_messages
from ...infrastructure.storage.storage import GlobalEventStore, MemoryStore
from ...infrastructure.tools import ToolRegistry, build_l1_tool_registry, run_registered_tool

logger = logging.getLogger(__name__)


class _UnsetToolRegistry:
    pass


_TOOL_REGISTRY_UNSET = _UnsetToolRegistry()


@dataclass
class ProactiveResult:
    """一次 L1 主动处理后的返回结果。"""

    message: str


class L1ProactiveService:
    """L1 主动唤醒流程服务，独立于 L0 用户对话链路。"""

    def __init__(
        self,
        store: GlobalEventStore,
        memory_store: MemoryStore,
        settings: Settings,
        *,
        tool_registry: ToolRegistry | None | _UnsetToolRegistry = _TOOL_REGISTRY_UNSET,
    ) -> None:
        self.store = store
        self.memory_store = memory_store
        self.settings = settings
        self.tool_registry = (
            build_l1_tool_registry(settings)
            if tool_registry is _TOOL_REGISTRY_UNSET
            else cast(ToolRegistry | None, tool_registry)
        )
        self.llm = OpenAICompatibleLLM(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def handle_event(self, event: dict[str, Any]) -> ProactiveResult:
        """处理一条 L1 主动事件，并把处理摘要写入当天共享上下文。"""
        level = str(event.get("attention_level") or "L1").strip().upper() or "L1"
        content = str(event.get("content") or "")

        logger.info("L1 主动事件处理开始 length=%s", len(content))

        # 先把触发事件存储。
        trigger_event = self.store.append_event(
            source=str(event.get("source") or ""),
            type=str(event.get("type") or ""),
            content=content,
            metadata=event.get("metadata"),
            attention_level=level,
        )

        event_date = _event_date(trigger_event)

        # 工具 schema
        tools_schema = (
            self.tool_registry.to_responses_tools()
            if self.tool_registry is not None and len(self.tool_registry) > 0
            else None
        )

        # 工具调用上限次数
        max_iterations = max(1, self.settings.max_tool_iterations)

        # 主动事件与工具调用循环：
        for i in range(max_iterations + 1):
            # 每轮都重新构建 prompt，因为上一轮工具调用和工具结果已经追加到了事件流。
            prompt = build_l1_messages(event_date, trigger_event)
            response = self.llm.complete(
                system=prompt.system,
                messages=prompt.messages,
                tools=tools_schema,
            )

            # 如果模型已经形成最终回复，没有工具调用请求。
            if not response.tool_calls:
                logger.info(
                    "L1 主动事件处理正常完成，response_length=%s",
                    len(response.content),
                )
                return self._finalize_natural_reply(trigger_event, response, level)

            # 如果已经达到工具调用次数上限。
            if i >= max_iterations:
                logger.warning(
                    "L1 主动事件的工具调用次数超限，停止循环 max=%s",
                    max_iterations,
                )
                return self._finalize_iterations_exhausted(trigger_event, level)

            # 如果模型有工具调用请求。全部执行模型请求的工具，并把每个工具结果落库
            logger.info(
                "L1 主动事件模型正在请求工具，工具调用数=%s",
                len(response.tool_calls),
            )

            # 执行模型请求的工具，并把每个工具结果落库
            self._dispatch_tool_calls(response.tool_calls, trigger_event, level)

    # 返回自然回复的落库和返回结果
    def _finalize_natural_reply(
        self,
        trigger_event: dict[str, Any],
        response: LLMResponse,
        level: str,
        *,
        context_status: str = "handled",
    ) -> ProactiveResult:
        """没有工具调用时，把模型自然语言输出落库并返回。"""
        message = response.content.strip()
        response_event = self.store.append_event(
            source="assistant",
            type="assistant_l1_response",
            content=message,
            metadata={
                "trigger_event_id": str(trigger_event.get("id", "")),
            },
            attention_level=level,
        )
        self._append_l1_context(
            trigger_event,
            response_event,
            message,
            status=context_status,
        )
        logger.info(
            "L1 主动事件处理完成 response_length=%s",
            len(message),
        )
        return ProactiveResult(message=message)

    # 工具调用循环超限的兜底落库和返回结果
    def _finalize_iterations_exhausted(
        self,
        trigger_event: dict[str, Any],
        level: str,
    ) -> ProactiveResult:
        """工具循环超限时返回的兜底结果，并在事件流里留痕。"""
        message = "工具调用次数已达上限，L1 主动处理已停止。"
        response_event = self.store.append_event(
            source="assistant",
            type="assistant_l1_response",
            content=message,
            metadata={
                "reason": "tool_iterations_exhausted",
                "trigger_event_id": str(trigger_event.get("id", "")),
            },
            attention_level=level,
        )
        self._append_l1_context(
            trigger_event,
            response_event,
            message,
            status="tool_iterations_exhausted",
        )
        return ProactiveResult(message=message)

    # 执行工具调用
    def _dispatch_tool_calls(
        self,
        tool_calls: tuple[ToolCall, ...],
        trigger_event: dict[str, Any],
        level: str,
    ) -> None:
        """逐个执行模型请求的工具调用，调用前后各落一条事件。"""
        trigger_event_id = str(trigger_event.get("id", ""))
        for call in tool_calls:
            metadata = {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "trigger_event_id": trigger_event_id,
            }
            self.store.append_event(
                source="assistant",
                type="assistant_tool_call",
                content=call.arguments,
                metadata=metadata,
                attention_level=level,
            )
            result_text = self._run_tool(call)
            self.store.append_event(
                source="tool",
                type="tool_result",
                content=result_text,
                metadata=metadata,
                attention_level=level,
            )

    # 执行单个工具，未知工具或异常都包装成 JSON 字符串结果
    def _run_tool(self, call: ToolCall) -> str:
        """执行单个工具，未知工具或异常都包装成 JSON 字符串结果。"""
        return run_registered_tool(self.tool_registry, call)

    # 把 L1 主动事件和 L1 响应的摘要写入当天共享上下文
    def _append_l1_context(
        self,
        trigger_event: dict[str, Any],
        response_event: dict[str, Any],
        response_content: str,
        *,
        status: str = "handled",
    ) -> None:
        event_date = _event_date(trigger_event)
        label = event_date.isoformat()
        trigger_id = str(trigger_event.get("id", ""))
        response_id = str(response_event.get("id", ""))
        self.memory_store.save_memory(
            kind="l1_context",
            label=label,
            period=_event_period(
                [trigger_event, response_event],
                fallback_date=event_date,
            ),
            content=_context_content(trigger_event, response_content),
            source_event_ids=[item for item in (trigger_id, response_id) if item],
            metadata={
                "event_source": trigger_event.get("source"),
                "event_type": trigger_event.get("type"),
                "trigger_event_id": trigger_id,
                "response_event_id": response_id,
                "status": status,
            },
        )

# 辅助函数
def _context_content(trigger_event: dict[str, Any], response_content: str) -> str:
    source = str(trigger_event.get("source", "")).strip() or "unknown"
    event_type = str(trigger_event.get("type", "")).strip() or "l1_trigger"
    trigger_content = str(trigger_event.get("content", "")).strip()
    response_text = response_content.strip()
    if response_text:
        return f"{source} {event_type}: {trigger_content}；AI 处理结果：{response_text}"
    return f"{source} {event_type}: {trigger_content}"

# 辅助函数：计算事件流的时间段
def _event_period(
    events: list[dict[str, Any]],
    *,
    fallback_date: date,
) -> dict[str, str]:
    label = fallback_date.isoformat()
    if not events:
        start = datetime.combine(fallback_date, datetime.min.time()).astimezone()
        return {
            "start": start.isoformat(timespec="seconds"),
            "end": (start + timedelta(seconds=1)).isoformat(timespec="seconds"),
            "label": label,
        }
    ordered = sorted(events, key=lambda item: str(item.get("timestamp", "")))
    start = _parse_datetime(str(ordered[0].get("timestamp", "")))
    end = _parse_datetime(str(ordered[-1].get("timestamp", ""))) + timedelta(seconds=1)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": label,
    }

# 辅助函数：从事件中解析日期
def _event_date(event: dict[str, object]) -> date:
    return _parse_datetime(str(event["timestamp"])).date()

# 辅助函数：解析 ISO 格式的时间字符串为本地时区 datetime
def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()
