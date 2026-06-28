from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

from ...attention.attention import AttentionLevel, parse_level
from ...config.config import Settings
from ...event.event import StandardEvent
from ...llm.llm import LLMResponse, OpenAICompatibleLLM, ToolCall
from ...prompt.prompt import build_l1_messages
from ...storage.storage import GlobalEventStore, MemoryStore
from ...tools import ToolRegistry, build_l1_tool_registry, run_registered_tool
from ...translation.translation import AIResponseType, translate_ai_response

logger = logging.getLogger(__name__)


class _UnsetToolRegistry:
    pass


_TOOL_REGISTRY_UNSET = _UnsetToolRegistry()


@dataclass
class ProactiveResult:
    """一次 L1 主动处理后的返回结果。"""

    message: str
    response_type: AIResponseType


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

    def handle_event(self, event: StandardEvent) -> ProactiveResult:
        """处理一条 L1 主动事件，并把处理摘要写入当天共享上下文。"""
        level = parse_level(event.attention_level)
        if level is not AttentionLevel.L1:
            raise ValueError(f"L1 主动流程只支持 L1 事件，收到: {event.attention_level}")

        logger.info(
            "L1 主动事件处理开始 source=%s type=%s length=%s",
            event.source,
            event.type,
            len(event.content),
        )

        trigger_event = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
            attention_level=level.value,
        )
        event_date = _event_date(trigger_event)
        response, context_status = self._complete_with_tools(event_date, trigger_event)
        translated = translate_ai_response(response.content)

        response_event = self.store.append_event(
            source="assistant",
            type="assistant_l1_response",
            content=translated.raw_response,
            metadata={
                "response_type": translated.response_type.value,
                "trigger_event_id": str(trigger_event.get("id", "")),
            },
            attention_level=level.value,
        )
        self._append_l1_context(
            trigger_event,
            response_event,
            translated.content,
            status=context_status,
        )

        logger.info(
            "L1 主动事件处理完成 response_type=%s response_length=%s",
            translated.response_type.value,
            len(translated.content),
        )
        return ProactiveResult(
            message=translated.content,
            response_type=translated.response_type,
        )

    def _complete_with_tools(
        self,
        event_date: date,
        trigger_event: dict[str, Any],
    ) -> tuple[LLMResponse, str]:
        """驱动 L1 工具调用循环，工具上下文只保留在本轮消息链里。"""
        prompt = build_l1_messages(event_date, trigger_event)
        messages = list(prompt.messages)
        tools_schema = (
            self.tool_registry.to_responses_tools()
            if self.tool_registry is not None and len(self.tool_registry) > 0
            else None
        )
        max_iterations = max(1, self.settings.max_tool_iterations)

        for iteration in range(max_iterations + 1):
            response = self.llm.complete(
                system=prompt.system,
                messages=messages,
                tools=tools_schema,
            )
            if not response.tool_calls:
                return response, "handled"

            if iteration >= max_iterations:
                logger.warning("L1 工具调用次数超限，停止循环 max=%s", max_iterations)
                return (
                    LLMResponse(content="工具调用次数已达上限，L1 主动处理已停止。"),
                    "tool_iterations_exhausted",
                )

            self._dispatch_tool_calls(response.tool_calls, messages)

        return (
            LLMResponse(content="工具调用次数已达上限，L1 主动处理已停止。"),
            "tool_iterations_exhausted",
        )

    def _dispatch_tool_calls(
        self,
        tool_calls: tuple[ToolCall, ...],
        messages: list[dict[str, Any]],
    ) -> None:
        """执行 L1 工具调用，结果同时落库并加入本轮 Responses 消息链。"""
        for call in tool_calls:
            call_id = call.id or f"l1_tool_call_{len(messages)}"
            self.store.append_event(
                source="assistant",
                type="assistant_tool_call",
                content=call.arguments,
                metadata={"tool_call_id": call_id, "tool_name": call.name},
                attention_level=AttentionLevel.L1.value,
            )
            messages.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
            )

            result_text = run_registered_tool(self.tool_registry, call)
            self.store.append_event(
                source="tool",
                type="tool_result",
                content=result_text,
                metadata={"tool_call_id": call_id, "tool_name": call.name},
                attention_level=AttentionLevel.L1.value,
            )
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_text,
                }
            )

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
            period=_event_period([trigger_event, response_event], fallback_date=event_date),
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


def _context_content(trigger_event: dict[str, Any], response_content: str) -> str:
    source = str(trigger_event.get("source", "")).strip() or "unknown"
    event_type = str(trigger_event.get("type", "")).strip() or "l1_trigger"
    trigger_content = str(trigger_event.get("content", "")).strip()
    response_text = response_content.strip()
    if response_text:
        return f"{source} {event_type}: {trigger_content}；AI 处理结果：{response_text}"
    return f"{source} {event_type}: {trigger_content}"


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


def _event_date(event: dict[str, object]) -> date:
    return _parse_datetime(str(event["timestamp"])).date()


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()
