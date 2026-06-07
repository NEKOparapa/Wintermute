from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.config import get_settings
from ..event.event import StandardEvent
from ..llm.llm import LLMResponse, ToolCall
from ..memory.consolidator import MemoryConsolidator
from ..prompt.prompt import build_l0_messages
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
        # 当前对话层只接收 L0 路由后的用户消息；其他事件类型需要先在上游分流。
        if event.type != "user_message":
            raise ValueError(f"暂不支持的事件类型: {event.type}")

        logger.info("L0 对话事件处理开始 length=%s", len(event.content))

        event = self._upload_local_attachments(event)

        # 先把用户输入写入全局事件流，后续 prompt、记忆整合都以事件流为事实来源。
        # 本地附件路径会在上一步上传并替换为 file_id，避免后续 prompt 重建时重复上传。
        user_event = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
            attention_level=event.attention_level,
        )
        # 根据本次用户事件所属日期自动整理会话记忆，并返回构建 prompt 时需要的日期范围。
        event_date = self.consolidator.auto_consolidate_session_for_event(user_event)

        settings = get_settings()
        # 只有在注册了工具时才把工具 schema 暴露给模型；否则模型只能自然语言回复。
        tools_schema = (
            self.tool_registry.to_responses_tools()
            if self.tool_registry is not None and len(self.tool_registry) > 0
            else None
        )
        max_iterations = max(1, settings.max_tool_iterations)

        # 工具调用循环：
        # 1. 用当前事件流构建 prompt；
        # 2. 请求 LLM；
        # 3. 如果 LLM 给出自然语言回复，落库并结束；
        # 4. 如果 LLM 请求工具，执行工具并把结果写回事件流，再进入下一轮让 LLM 读取结果。
        for iteration in range(max_iterations + 1):
            # 每轮都重新构建 prompt，因为上一轮工具调用和工具结果已经追加到了事件流。
            prompt = build_l0_messages(event_date)
            response = self.llm.complete(
                system=prompt.system,
                messages=prompt.messages,
                tools=tools_schema,
            )

            # 没有工具调用表示模型已经形成最终回复，可以结束本次 turn。
            if not response.tool_calls:
                return self._finalize_natural_reply(response)

            # 已经达到工具调用上限时不再执行新工具，避免模型反复调用工具造成死循环。
            if iteration >= max_iterations:
                logger.warning("工具调用次数超限，停止循环 max=%s", max_iterations)
                break

            # 执行模型请求的工具，并把每个工具结果落库；下一轮 prompt 会带上这些结果。
            self._dispatch_tool_calls(response.tool_calls)

        return self._finalize_iterations_exhausted()

    def _finalize_natural_reply(self, response: LLMResponse) -> TurnResult:
        """没有工具调用时，把模型自然语言输出落库并返回。"""
        # LLM 原始输出可能带有响应类型标记，这里统一翻译成前端可展示的文本和事件类型。
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
        # 即使异常结束，也写入一条助手事件，保证事件流能还原本次 turn 的终止原因。
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
        # 找不到工具时不抛异常，而是返回结构化错误，让模型有机会给用户解释或换方案。
        tool = self.tool_registry.get(call.name) if self.tool_registry is not None else None
        if tool is None:
            return json.dumps(
                {"error": f"unknown_tool: {call.name}"},
                ensure_ascii=False,
            )

        try:
            # 工具参数来自模型输出，必须先解析并确认是对象，避免把非法参数传给工具层。
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
            # 工具失败只影响本次工具结果，不让异常穿透到 HTTP 层导致整轮对话崩掉。
            logger.exception("工具执行异常 name=%s", call.name)
            return json.dumps(
                {"error": "tool_exception", "message": str(exc)},
                ensure_ascii=False,
            )

    def _upload_local_attachments(self, event: StandardEvent) -> StandardEvent:
        """把 metadata.attachments 中的本地 path 上传为 file_id。"""
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list):
            return event

        settings = get_settings()
        next_attachments: list[object] = []
        changed = False
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                next_attachments.append(attachment)
                continue

            local_path = _local_attachment_path(attachment)
            next_attachment = dict(attachment)
            if not local_path:
                next_attachments.append(next_attachment)
                continue

            changed = True
            for key in ("path", "file_path", "local_path"):
                next_attachment.pop(key, None)

            if next_attachment.get("file_id"):
                next_attachments.append(next_attachment)
                continue

            resolved_path = Path(local_path).expanduser()
            if not resolved_path.is_absolute():
                resolved_path = Path.cwd() / resolved_path
            if not resolved_path.is_file():
                raise ValueError(f"attachments[{index}].path 文件不存在: {local_path}")

            logger.info("上传本地附件 path=%s kind=%s", resolved_path, next_attachment.get("kind"))
            uploaded = self.llm.upload_file(
                resolved_path,
                preprocess_configs=_attachment_preprocess_configs(next_attachment),
                poll_interval_seconds=settings.file_upload_poll_interval_seconds,
                wait_timeout_seconds=settings.file_upload_timeout_seconds,
            )
            next_attachment["file_id"] = uploaded.id
            next_attachment.setdefault("filename", resolved_path.name)
            next_attachment.pop("preprocess_configs", None)
            next_attachments.append(next_attachment)

        if not changed:
            return event

        next_metadata = dict(metadata)
        next_metadata["attachments"] = next_attachments
        return StandardEvent(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=next_metadata,
            attention_level=event.attention_level,
        )


def _local_attachment_path(attachment: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "local_path"):
        value = attachment.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


def _attachment_preprocess_configs(attachment: dict[str, Any]) -> dict[str, Any] | None:
    value = attachment.get("preprocess_configs")
    return value if isinstance(value, dict) and value else None
