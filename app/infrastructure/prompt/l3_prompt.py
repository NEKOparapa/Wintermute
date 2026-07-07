from __future__ import annotations

from typing import Any

from .messages import build_events_input_message
from .types import PromptContent

_L3_SYSTEM_PROMPT = """你负责把 L3 背景事件压缩成一到两行中文记忆，供未来对话参考。

L3 是低优先级背景、环境或日志类事件。

要求：
- 只输出摘要正文，最多两行，不加序号或前缀。
- 只保留长期或后续对话可能有参考价值的关键事实。
- 弱化临时状态、重复日志、瞬时噪音和无行动价值的细节。
- 不寒暄、不解释、不编造原始事件中没有的信息。
"""


def build_l3_event_summary_prompt(event: dict[str, Any]) -> PromptContent:
    """构建 L3 背景事件压缩 prompt。"""
    # 单条 L3 背景事件只压缩成事件记忆，不产生对话回复。
    return PromptContent(
        system=_L3_SYSTEM_PROMPT,
        messages=[
            build_events_input_message(
                f"请把这条 L3 背景事件压缩成一到两行：\n\n{_format_events([event])}",
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
