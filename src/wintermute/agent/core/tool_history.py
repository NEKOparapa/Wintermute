"""在模型回放边界投影旧后台任务标识，原始工具审计留在历史中。"""

import hashlib
import json
import re
from dataclasses import replace
from typing import Any, overload

from wintermute.agent.core.types import AssistantMessage, ToolCall, ToolResultMessage

_LEGACY_TASK_ID = re.compile(r"subagent_[0-9a-f]{32}")
_STATUS_TOOLS = {"subagent_status", "subagent_cancel"}
_RECEIPT_TOOLS = {"subagent_start", *_STATUS_TOOLS}
_RESPONSES_OUTPUT = "openai.responses.output"


def _archive_reference(value: object) -> str | None:
    if not isinstance(value, str) or not _LEGACY_TASK_ID.fullmatch(value):
        return None
    return "archived-" + hashlib.sha256(value.encode("ascii")).hexdigest()[:16]


def _project_calls(message: AssistantMessage) -> AssistantMessage:
    changed: dict[str, ToolCall] = {}
    for call in message.tool_calls:
        if call.name in _STATUS_TOOLS:
            alias = _archive_reference(call.arguments.get("task_id"))
            if alias is not None:
                changed[call.id] = replace(call, arguments={**call.arguments, "task_id": alias})
    if not changed:
        return message
    calls = tuple(changed.get(call.id, call) for call in message.tool_calls)
    metadata = message.provider_data
    output = metadata.get(_RESPONSES_OUTPUT)
    if isinstance(output, list):
        # Responses 会将原始推理项与函数调用整组回传。只更改受影响的参数副本，
        # 同时保留对原始参数不一致的检查；不能靠投影修复一个已损坏的原始批次。
        originals = {call.id: call for call in message.tool_calls}
        projected: list[Any] = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = item.get("call_id")
                replacement = changed.get(call_id) if isinstance(call_id, str) else None
                if replacement is not None and item.get("name") == replacement.name:
                    arguments = item.get("arguments")
                    try:
                        decoded = json.loads(arguments) if isinstance(arguments, str) else None
                    except ValueError:
                        decoded = None
                    if decoded == originals[replacement.id].arguments:
                        item = {
                            **item,
                            "arguments": json.dumps(
                                replacement.arguments, ensure_ascii=False, allow_nan=False
                            ),
                        }
            projected.append(item)
        metadata = {**metadata, _RESPONSES_OUTPUT: projected}
    return replace(message, tool_calls=calls, provider_data=metadata)


@overload
def model_tool_message(message: AssistantMessage) -> AssistantMessage: ...


@overload
def model_tool_message(message: ToolResultMessage) -> ToolResultMessage: ...


def model_tool_message(
    message: AssistantMessage | ToolResultMessage,
) -> AssistantMessage | ToolResultMessage:
    """历史上的后台任务用只读归档引用表示；新工具结果按原样回放。"""
    if isinstance(message, AssistantMessage):
        return _project_calls(message)
    if message.name not in _RECEIPT_TOOLS or "subagent_" not in message.content:
        return message
    try:
        payload = json.loads(message.content)
    except ValueError:
        # 旧的未知任务错误也可能回显不存在的 ID。普通工具结果只处理协议字段。
        if message.is_error:
            text = _LEGACY_TASK_ID.sub(
                lambda match: _archive_reference(match[0]) or match[0], message.content
            )
            if text != message.content:
                return replace(message, content=text)
        return message
    if not isinstance(payload, dict):
        return message
    tasks = payload.get("tasks", [payload.get("task")])
    if not isinstance(tasks, list):
        return message
    changed = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        alias = _archive_reference(task.get("task_id"))
        if alias is not None:
            changed = True
            task["task_id"] = alias
            task.pop("call_id", None)
            if task.get("notification_error"):
                task["notification_error"] = "Notification failed"
    if not changed:
        return message
    return replace(message, content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
