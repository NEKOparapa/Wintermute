from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptContent:
    """发送给 LLM 前的提示内容，系统提示词和历史消息分开保存。"""

    system: str
    messages: list[dict[str, Any]]
