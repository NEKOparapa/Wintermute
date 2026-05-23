from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。
"""


def build_messages(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把全局历史事件转换成发送给 LLM 的 messages。"""
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for event in events:
        if event.get("type") == "user_message":
            messages.append({"role": "user", "content": str(event.get("content", ""))})
        elif event.get("type") == "assistant_response":
            messages.append({"role": "assistant", "content": str(event.get("content", ""))})
    return messages
