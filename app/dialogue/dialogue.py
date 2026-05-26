from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..config.config import get_settings
from ..event.event import StandardEvent
from ..llm.llm import LLMResponse, ToolCall
from ..memory.consolidator import MemoryConsolidator
from ..prompt.prompt import build_messages
from ..storage.storage import GlobalEventStore
from ..tools import ToolRegistry
from ..translation.translation import AIResponseType, assistant_event_type, translate_ai_response

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """一次对话处理后的返回结果。"""

    message: str
    response_type: AIResponseType


class DialogueService:
    """对话流程服务，负责处理 L0 用户消息事件，并驱动工具调用循环。"""

    def __init__(
        self,
        store: GlobalEventStore,
        llm,
        consolidator: MemoryConsolidator,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        """注入历史存储、LLM 客户端与可选工具注册表。"""
        self.store = store
        self.llm = llm
        self.consolidator = consolidator
        self.tool_registry = tool_registry

    def handle_event(self, event: StandardEvent) -> TurnResult:
        """处理一条 L0 用户消息事件，必要时驱动工具调用，最终返回助手回复。"""
        if event.type != "user_message":
            raise ValueError(f"暂不支持的事件类型: {event.type}")

        logger.info("L0 对话事件处理开始 length=%s", len(event.content))

        user_event = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
        )
        event_date = self.consolidator.auto_consolidate_session_for_event(user_event)

        settings = get_settings()
        tools_schema = (
            self.tool_registry.to_openai_tools()
            if self.tool_registry is not None and len(self.tool_registry) > 0
            else None
        )
        max_iterations = max(1, settings.max_tool_iterations)

        for iteration in range(max_iterations + 1):
            prompt = build_messages(event_date)
            response = self.llm.complete(
                system=prompt.system,
                messages=prompt.messages,
                tools=tools_schema,
            )

            if not response.tool_calls:
                return self._finalize_natural_reply(response)

            if iteration >= max_iterations:
                logger.warning("工具调用次数超限，停止循环 max=%s", max_iterations)
                break

            self._dispatch_tool_calls(response.tool_calls)

        return self._finalize_iterations_exhausted()

    def _finalize_natural_reply(self, response: LLMResponse) -> TurnResult:
        """没有工具调用时，把模型自然语言输出落库并返回。"""
        translated = translate_ai_response(response.content)
        self.store.append_event(
            source="assistant",
            type=assistant_event_type(translated.response_type),
            content=translated.raw_response,
            metadata={"response_type": translated.response_type.value},
        )
        logger.info(
            "L0 对话事件处理完成 response_type=%s response_length=%s",
            translated.response_type.value,
            len(translated.content),
        )
        return TurnResult(
            message=translated.content,
            response_type=translated.response_type,
        )

    def _finalize_iterations_exhausted(self) -> TurnResult:
        """工具循环超限时返回的兜底结果，并在事件流里留痕。"""
        message = "工具调用次数已达上限，对话已停止。"
        self.store.append_event(
            source="assistant",
            type="assistant_natural_response",
            content=message,
            metadata={"response_type": AIResponseType.NATURAL_REPLY.value, "reason": "tool_iterations_exhausted"},
        )
        return TurnResult(message=message, response_type=AIResponseType.NATURAL_REPLY)

    def _dispatch_tool_calls(self, tool_calls: tuple[ToolCall, ...]) -> None:
        """逐个执行模型请求的工具调用，调用前后各落一条事件。"""
        for call in tool_calls:
            self.store.append_event(
                source="assistant",
                type="assistant_tool_call",
                content=call.arguments,
                metadata={"tool_call_id": call.id, "tool_name": call.name},
            )
            result_text = self._run_tool(call)
            self.store.append_event(
                source="tool",
                type="tool_result",
                content=result_text,
                metadata={"tool_call_id": call.id, "tool_name": call.name},
            )

    def _run_tool(self, call: ToolCall) -> str:
        """执行单个工具，未知工具或异常都包装成 JSON 字符串结果。"""
        tool = self.tool_registry.get(call.name) if self.tool_registry is not None else None
        if tool is None:
            return json.dumps(
                {"error": f"unknown_tool: {call.name}"},
                ensure_ascii=False,
            )

        try:
            arguments = json.loads(call.arguments) if call.arguments else {}
        except json.JSONDecodeError:
            return json.dumps(
                {"error": "invalid_arguments_json", "raw": call.arguments},
                ensure_ascii=False,
            )
        if not isinstance(arguments, dict):
            return json.dumps(
                {"error": "arguments_not_object"},
                ensure_ascii=False,
            )

        try:
            return tool.run(arguments)
        except Exception as exc:  # noqa: BLE001 - 工具异常不应中断对话
            logger.exception("工具执行异常 name=%s", call.name)
            return json.dumps(
                {"error": "tool_exception", "message": str(exc)},
                ensure_ascii=False,
            )
