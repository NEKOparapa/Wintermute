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


@dataclass(frozen=True)
class AttentionRoute:
    """标准事件进入注意力层后的路由结果。"""

    level: AttentionLevel
    event: StandardEvent


def route_event(event: StandardEvent) -> AttentionRoute | None:
    """把标准事件路由到注意力层；当前只有用户对话进入 L0。"""
    if event.type == "user_message":
        return AttentionRoute(level=AttentionLevel.L0, event=event)
    return None
