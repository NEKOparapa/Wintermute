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

    DIALOGUE = "dialogue"  # L0 / L1：进入会话流程，唤起主 AI 对话。
    BACKGROUND = "background"  # L2 / L3：只压缩进事件记忆，不进行对话。


# 会话通道与背景通道的等级划分；L0/L1 对话，L2/L3 只压缩进记忆。
_DIALOGUE_LEVELS = frozenset({AttentionLevel.L0, AttentionLevel.L1})


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
    """按事件自带的注意力等级分流：L0/L1 进对话，L2/L3 进背景事件流程。"""
    level = parse_level(event.attention_level)
    channel = (
        AttentionChannel.DIALOGUE if level in _DIALOGUE_LEVELS else AttentionChannel.BACKGROUND
    )
    return AttentionRoute(level=level, channel=channel, event=event)
