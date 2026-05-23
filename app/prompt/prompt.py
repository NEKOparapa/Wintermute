from __future__ import annotations

from dataclasses import dataclass

_SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。
"""


@dataclass(frozen=True)
class PromptContent:
    """发送给 LLM 前的提示内容，系统提示词和历史消息分开保存。"""

    system: str
    messages: list[dict[str, str]]


def build_messages(events: list[dict[str, object]]) -> PromptContent:
    """根据历史事件返回系统提示词和对话 messages。"""
    messages = _history_messages(events)
    return PromptContent(
        system=_SYSTEM_PROMPT,
        messages=messages,
    )


def _history_messages(events: list[dict[str, object]]) -> list[dict[str, str]]:
    """把历史事件转换成 LLM 对话消息。"""
    messages: list[dict[str, str]] = []
    for event in events:
        if event.get("type") == "user_message":
            messages.append({"role": "user", "content": str(event.get("content", ""))})
        elif event.get("type") == "assistant_response":
            messages.append({"role": "assistant", "content": str(event.get("content", ""))})
    return messages
