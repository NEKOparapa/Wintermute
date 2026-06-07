from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..event.event import StandardEvent


class AttentionLevel(Enum):
    """注意力层级。"""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class AttentionChannel(Enum):
    """事件按注意力等级分流到的处理通道。"""

    DIALOGUE = "dialogue"  # L0：用户主动对话。
    PROACTIVE = "proactive"  # L1：外部事件主动唤醒主 AI。
    BACKGROUND = "background"  # L2 / L3：只压缩进事件记忆，不进行对话。


# L0/L1 是两条独立的主 AI 链路；L2/L3 只压缩进记忆。
_DIALOGUE_LEVELS = frozenset({AttentionLevel.L0})
_PROACTIVE_LEVELS = frozenset({AttentionLevel.L1})


@dataclass(frozen=True)
class AttentionRoute:
    """标准事件进入注意力层后的路由结果。"""

    level: AttentionLevel
    channel: AttentionChannel
    event: StandardEvent


def parse_level(value: object) -> AttentionLevel:
    """把调用方给的等级字符串解析成 AttentionLevel，非法值抛 ValueError。"""
    if isinstance(value, AttentionLevel):
        return value
    text = str(value or "").strip().upper()
    try:
        return AttentionLevel(text)
    except ValueError:
        valid = ", ".join(level.value for level in AttentionLevel)
        raise ValueError(f"无效的注意力等级: {value!r}，支持 {valid}。") from None


def route_event(event: StandardEvent) -> AttentionRoute:
    """按事件自带的注意力等级分流：L0 对话，L1 主动唤醒，L2/L3 背景流程。"""
    level = parse_level(event.attention_level)
    if level in _DIALOGUE_LEVELS:
        channel = AttentionChannel.DIALOGUE
    elif level in _PROACTIVE_LEVELS:
        channel = AttentionChannel.PROACTIVE
    else:
        channel = AttentionChannel.BACKGROUND
    return AttentionRoute(level=level, channel=channel, event=event)
