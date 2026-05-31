from __future__ import annotations

from typing import Any

import tiktoken

TOKEN_ENCODING = "cl100k_base"

# 多模态媒体 part（图片 / 音频 / 视频 / 文件）的粗略 token 估算值，
# 仅用于 prompt 预算裁剪，避免把 base64 原文当成文本计入。
_MEDIA_TOKEN_ESTIMATE = 1024


def get_encoding():
    """获取固定 token encoding。"""
    return tiktoken.get_encoding(TOKEN_ENCODING)


def count_text_tokens(text: str) -> int:
    """估算一段文本的 token 数。"""
    return len(get_encoding().encode(text))


def count_message_tokens(messages: list[dict[str, Any]]) -> int:
    """估算 Responses input 项的 token 数，包含少量结构开销。

    兼容纯文本消息、多模态 content 列表，以及 function_call /
    function_call_output 项；base64 媒体原文不计入文本 token，而是按固定
    估算值计费，避免把超长 base64 误算进 prompt 预算。
    """
    encoding = get_encoding()
    total = 0
    for message in messages:
        total += 4
        total += _count_item_tokens(message, encoding)
    return total + 2


def _count_item_tokens(item: Any, encoding: Any) -> int:
    """统计单个 input 项的 token，跳过 base64 等二进制字段。"""
    if not isinstance(item, dict):
        return len(encoding.encode(str(item)))

    total = 0
    for key, value in item.items():
        if key == "content":
            total += _count_content_tokens(value, encoding)
        elif key in {"arguments", "output", "name", "role", "type", "call_id"} and isinstance(value, str):
            total += len(encoding.encode(value))
    return total


def _count_content_tokens(content: Any, encoding: Any) -> int:
    """统计消息 content 的 token：字符串直接编码，列表逐 part 处理。"""
    if isinstance(content, str):
        return len(encoding.encode(content))
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if not isinstance(part, dict):
            total += len(encoding.encode(str(part)))
            continue
        if part.get("type") in {"input_text", "output_text"}:
            total += len(encoding.encode(str(part.get("text", ""))))
        else:
            # 图片 / 音频 / 视频 / 文件等媒体 part：用固定估算值，不计 base64 原文。
            total += _MEDIA_TOKEN_ESTIMATE
    return total


def count_event_tokens(events: list[dict[str, Any]]) -> int:
    """按事件 content 估算 token 数。"""
    encoding = get_encoding()
    return sum(len(encoding.encode(str(event.get("content", "")))) for event in events)
