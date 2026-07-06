from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from ...config.config import Settings
from ...infrastructure.llm.llm import LLMResponse, OpenAICompatibleLLM, ToolCall
from ...memory.consolidator import MemoryConsolidator
from ...infrastructure.prompt.l0_prompt import build_l0_messages
from ...infrastructure.storage.storage import GlobalEventStore
from ...infrastructure.tools import ToolRegistry, build_l0_tool_registry, run_registered_tool

logger = logging.getLogger(__name__)


class _UnsetToolRegistry:
    pass


_TOOL_REGISTRY_UNSET = _UnsetToolRegistry()


@dataclass
class TurnResult:
    """一次对话处理后的返回结果。"""

    message: str


class DialogueService:
    """对话流程服务，负责处理 L0 用户消息事件，并驱动工具调用循环。"""

    def __init__(
        self,
        store: GlobalEventStore,
        consolidator: MemoryConsolidator,
        settings: Settings,
        *,
        tool_registry: ToolRegistry | None | _UnsetToolRegistry = _TOOL_REGISTRY_UNSET,
    ) -> None:
        """注入历史存储、记忆压缩器与可选工具注册表。"""
        self.store = store
        self.consolidator = consolidator
        self.settings = settings
        self.tool_registry = (
            build_l0_tool_registry(settings)
            if tool_registry is _TOOL_REGISTRY_UNSET
            else cast(ToolRegistry | None, tool_registry)
        )
        self.llm = OpenAICompatibleLLM(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def handle_event(self, event: dict[str, Any]) -> TurnResult:
        """处理一条 L0 用户消息事件，必要时驱动工具调用，最终返回助手回复。"""
        content = str(event.get("content") or "")

        logger.info("L0 对话事件处理开始 length=%s", len(content))

        # 先把用户输入存储
        user_event = self.store.append_event(
            source=str(event.get("source") or ""),
            type=str(event.get("type") or ""),
            content=content,
            metadata=event.get("metadata"),
            attention_level=str(event.get("attention_level") or "L0"),
        )
        # 根据本次用户事件所属日期自动整理会话记忆，并返回构建 prompt 时需要的日期范围。
        event_date = self.consolidator.auto_consolidate_session_for_event(user_event)

        # 工具 schema
        tools_schema = (
            self.tool_registry.to_responses_tools()
            if self.tool_registry is not None and len(self.tool_registry) > 0
            else None
        )

        # 工具调用上限次数
        max_iterations = max(1, self.settings.max_tool_iterations)

        # 对话与工具调用循环：
        for i in range(max_iterations + 1):
            # 每轮都重新构建 prompt，因为上一轮工具调用和工具结果已经追加到了事件流。
            prompt = build_l0_messages(event_date)
            response = self.llm.complete(
                system=prompt.system,
                messages=prompt.messages,
                tools=tools_schema,
            )

            # 如果模型已经形成最终回复，没有工具调用请求。
            if not response.tool_calls:
                logger.info("L0 对话事件处理正常完成，response_length=%s", len(response.content))
                return self._finalize_natural_reply(response)

            # 如果已经达到工具调用次数上限。
            if i >= max_iterations:
                logger.warning("L0 对话事件的工具调用次数超限，停止循环 max=%s", max_iterations)
                return self._finalize_iterations_exhausted()

            # 如果模型有工具调用请求。全部执行模型请求的工具，并把每个工具结果落库
            logger.info("L0 对话事件模型正在请求工具，工具调用数  =%s", len(response.tool_calls))
            self._dispatch_tool_calls(response.tool_calls)


    def _finalize_natural_reply(self, response: LLMResponse) -> TurnResult:
        """没有工具调用时，把模型自然语言输出落库并返回。"""
        message = response.content.strip()
        self.store.append_event(
            source="assistant",
            type="assistant_natural_response",
            content=message,
        )
        logger.info(
            "L0 对话事件处理完成 response_length=%s",
            len(message),
        )
        return TurnResult(message=message)

    def _finalize_iterations_exhausted(self) -> TurnResult:
        """工具循环超限时返回的兜底结果，并在事件流里留痕。"""
        message = "工具调用次数已达上限，对话已停止。"
        # 即使异常结束，也写入一条助手事件，保证事件流能还原本次 turn 的终止原因。
        self.store.append_event(
            source="assistant",
            type="assistant_natural_response",
            content=message,
            metadata={"reason": "tool_iterations_exhausted"},
        )
        return TurnResult(message=message)

    def _dispatch_tool_calls(self, tool_calls: tuple[ToolCall, ...]) -> None:
        """逐个执行模型请求的工具调用，调用前后各落一条事件。"""
        for call in tool_calls:
            # 先记录“模型请求了哪个工具和参数”，这样即使工具失败也能追踪模型决策。
            self.store.append_event(
                source="assistant",
                type="assistant_tool_call",
                content=call.arguments,
                metadata={"tool_call_id": call.id, "tool_name": call.name},
            )
            result_text = self._run_tool(call)
            # 再记录工具执行结果，下一轮 LLM 会通过 prompt 读到这条 tool_result。
            self.store.append_event(
                source="tool",
                type="tool_result",
                content=result_text,
                metadata={"tool_call_id": call.id, "tool_name": call.name},
            )

    def _run_tool(self, call: ToolCall) -> str:
        """执行单个工具，未知工具或异常都包装成 JSON 字符串结果。"""
        return run_registered_tool(self.tool_registry, call)
