from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


def assistant_event_type(response_type: AIResponseType) -> str:
    """把翻译层分类映射为助手历史事件类型。"""
    if response_type is AIResponseType.NATURAL_REPLY:
        return "assistant_natural_response"
    if response_type is AIResponseType.QUESTION:
        return "assistant_question"
    return "assistant_tool_call"


def translate_ai_response(response: str) -> TranslationResult:
    """识别 AI 自然语言回复类型。工具调用走原生 tools 协议，不在此处理。"""
    text = response.strip()
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
