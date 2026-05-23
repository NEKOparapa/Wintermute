from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AIResponseType(Enum):
    """AI 回复进入翻译层后的分类。"""

    NATURAL_REPLY = "natural_reply"
    TOOL_CALL = "tool_call"
    QUESTION = "question"


@dataclass(frozen=True)
class TranslationResult:
    """翻译层输出的标准结果。"""

    response_type: AIResponseType
    content: str
    raw_response: str
    payload: dict[str, Any] | None = None


def assistant_event_type(response_type: AIResponseType) -> str:
    """把翻译层分类映射为助手历史事件类型。"""
    if response_type is AIResponseType.NATURAL_REPLY:
        return "assistant_natural_response"
    if response_type is AIResponseType.QUESTION:
        return "assistant_question"
    return "assistant_tool_call"


def translate_ai_response(response: str) -> TranslationResult:
    """识别 AI 回复类型，并返回后续流程需要的标准结果。"""
    text = response.strip()
    payload = _json_object(text)
    if payload is not None and _is_tool_call(payload):
        return TranslationResult(
            response_type=AIResponseType.TOOL_CALL,
            content="",
            raw_response=text,
            payload=payload,
        )

    if text.endswith(("?", "？")):
        return TranslationResult(
            response_type=AIResponseType.QUESTION,
            content=text,
            raw_response=text,
        )

    return TranslationResult(
        response_type=AIResponseType.NATURAL_REPLY,
        content=text,
        raw_response=text,
    )


def _json_object(text: str) -> dict[str, Any] | None:
    """只接受 JSON 对象，其他 JSON 类型不作为工具调用协议。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _is_tool_call(payload: dict[str, Any]) -> bool:
    """按 v1 启发式判断 JSON 对象是否表达工具调用。"""
    if payload.get("type") == AIResponseType.TOOL_CALL.value:
        return True
    if payload.get("kind") == AIResponseType.TOOL_CALL.value:
        return True

    tool_fields = {"tool", "tool_name", "action", "arguments"}
    return bool(tool_fields.intersection(payload))
