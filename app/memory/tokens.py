from __future__ import annotations

from typing import Any

import tiktoken

TOKEN_ENCODING = "cl100k_base"


def get_encoding():
    """获取固定 token encoding。"""
    return tiktoken.get_encoding(TOKEN_ENCODING)


def count_text_tokens(text: str) -> int:
    """估算一段文本的 token 数。"""
    return len(get_encoding().encode(text))


def count_message_tokens(messages: list[dict[str, str]]) -> int:
    """估算 chat messages 的 token 数，包含少量结构开销。"""
    encoding = get_encoding()
    total = 0
    for message in messages:
        total += 4
        for key, value in message.items():
            total += len(encoding.encode(str(value)))
            if key == "name":
                total -= 1
    return total + 2


def count_event_tokens(events: list[dict[str, Any]]) -> int:
    """按事件 content 估算 token 数。"""
    encoding = get_encoding()
    return sum(len(encoding.encode(str(event.get("content", "")))) for event in events)
