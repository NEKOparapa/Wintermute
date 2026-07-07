from __future__ import annotations

from typing import Any

from .messages import build_events_input_message
from .types import PromptContent

_L2_SYSTEM_PROMPT = """你负责把 L2 背景事件压缩成一到两行中文记忆，供未来对话参考。

L2 是较重要但不需要立即主动打断用户的背景事件。

要求：
- 只输出摘要正文，最多两行，不加序号或前缀。
- 保留时间、来源、关键事实，以及可能影响用户近期决策的上下文。
- 弱化一次性噪音和过程细节。
- 不寒暄、不解释、不编造原始事件中没有的信息。
"""


def build_l2_event_summary_prompt(event: dict[str, Any]) -> PromptContent:
    """构建 L2 背景事件压缩 prompt。"""
    # 单条 L2 背景事件只压缩成事件记忆，不产生对话回复。
    return PromptContent(
        system=_L2_SYSTEM_PROMPT,
        messages=[
            build_events_input_message(
                f"请把这条 L2 背景事件压缩成一到两行：\n\n{_format_events([event])}",
                [event],
            )
        ],
    )


# 格式化原始事件，方便模型在纯文本中看到时间、来源、类型和内容。
def _format_events(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events:
        lines.append(
            f"- {event.get('timestamp')} {event.get('source')} {event.get('type')}: "
            f"{event.get('content', '')}"
        )
    return "\n".join(lines)
