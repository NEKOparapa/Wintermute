from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StandardEvent:
    """事件层输出的标准事件格式。"""

    source: str
    type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_message_event(message: str) -> StandardEvent:
    """把外部输入消息归一化成用户消息事件。"""
    text = message.strip()
    if not text:
        raise ValueError("message 不能为空。")
    return StandardEvent(
        source="user",
        type="user_message",
        content=text,
    )
