from __future__ import annotations

from typing import Any


DIALOGUE_EVENT_TYPES = frozenset(
    {
        "user_message",
        "assistant_response",
        "assistant_natural_response",
        "assistant_tool_call",
        "tool_result",
    }
)


def build_event_input_message(text: str, event: dict[str, object]) -> dict[str, Any]:
    """Build one Responses user input item from an event and its attachments."""
    return _user_input_message(text, _event_attachments(event))


def build_events_input_message(
    text: str,
    events: list[dict[str, object]],
) -> dict[str, Any]:
    """Build one Responses user input item from multiple events and attachments."""
    attachments: list[object] = []
    for event in events:
        attachments.extend(_event_attachments(event))
    return _user_input_message(text, attachments)


def build_history_messages(events: list[dict[str, object]]) -> list[dict[str, Any]]:
    """把历史事件转换成 Responses API 的 input 项列表。

    保留工具调用（function_call）与工具结果（function_call_output）以维持
    原生 tools 协议；用户消息可携带图片 / 音频 / 视频 / 文件等多模态附件。
    """
    messages: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        content = str(event.get("content", ""))
        if event_type == "user_message":
            messages.append(_user_input_message(content, metadata.get("attachments")))
        elif event_type in {
            "assistant_response",
            "assistant_natural_response",
        }:
            messages.append({"role": "assistant", "content": content})
        elif event_type == "assistant_tool_call":
            messages.append(
                {
                    "type": "function_call",
                    "call_id": str(metadata.get("tool_call_id", "")),
                    "name": str(metadata.get("tool_name", "")),
                    "arguments": content,
                }
            )
        elif event_type == "tool_result":
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": str(metadata.get("tool_call_id", "")),
                    "output": content,
                }
            )
    return messages


def is_dialogue_event(event: dict[str, object]) -> bool:
    """判断事件是否属于对话事件，覆盖用户消息、助手回复与工具调用/结果。"""
    if str(event.get("attention_level", "")).upper() == "L1":
        return False
    return event.get("type") in DIALOGUE_EVENT_TYPES


def _user_input_message(text: str, attachments: object) -> dict[str, Any]:
    """构建一条用户 input 消息。无附件时用纯文本，否则用多模态 content 列表。"""
    if not isinstance(attachments, list) or not attachments:
        return {"role": "user", "content": text}

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "input_text", "text": text})
    for attachment in attachments:
        part = _attachment_content_part(attachment)
        if part is not None:
            parts.append(part)
    if not parts:
        parts.append({"type": "input_text", "text": text})
    return {"role": "user", "content": parts}


def _attachment_content_part(attachment: object) -> dict[str, Any] | None:
    """把一条附件字典映射成 Responses API 的 content part；无法表达时返回 None。"""
    if not isinstance(attachment, dict):
        return None

    kind = str(attachment.get("kind", "")).strip().lower()
    url = _opt(attachment.get("url"))
    data = _opt(attachment.get("data"))
    file_id = _opt(attachment.get("file_id"))
    mime = _opt(attachment.get("mime_type"))
    raw_part = attachment.get("content_part")
    if isinstance(raw_part, dict) and not file_id:
        return dict(raw_part)

    if kind == "image":
        part: dict[str, Any] = {
            "type": "input_image",
        }
        detail = _opt(attachment.get("detail"))
        if detail:
            part["detail"] = detail
        if file_id:
            part["file_id"] = file_id
        elif url:
            part["image_url"] = url
        elif data:
            part["image_url"] = _data_url(mime or "image/png", data)
        else:
            return None
        return part

    if kind == "audio":
        if file_id:
            return {"type": "input_audio", "file_id": file_id}
        if url:
            return {"type": "input_audio", "audio_url": url}
        if data:
            audio_mime = mime or _mime_from_data_url(data) or _audio_mime_from_format(
                _opt(attachment.get("format"))
            )
            return {
                "type": "input_audio",
                "audio_url": _data_url(audio_mime or "audio/mpeg", data),
            }
        return None

    if kind == "file":
        if file_id:
            return {"type": "input_file", "file_id": file_id}
        if url:
            return {"type": "input_file", "file_url": url}
        if data:
            return {
                "type": "input_file",
                "filename": _opt(attachment.get("filename")) or "file",
                "file_data": _data_url(mime or "application/octet-stream", data),
            }
        return None

    if kind == "video":
        # OpenAI 官方 Responses API 暂未原生支持视频，这里按兼容服务常见约定尽力表达。
        if file_id:
            return {"type": "input_video", "file_id": file_id}
        if url:
            return {"type": "input_video", "video_url": url}
        if data:
            return {"type": "input_video", "video_url": _data_url(mime or "video/mp4", data)}
        return None

    # content_part 直通：兼容服务需要特殊结构时，直接使用调用方给出的原始 part。
    if isinstance(raw_part, dict):
        return dict(raw_part)

    return None


def _event_attachments(event: dict[str, object]) -> list[object]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
    return list(attachments) if isinstance(attachments, list) else []


def _data_url(mime: str, data: str) -> str:
    """把 base64 原文包装成 data URL；已是 data URL 时原样返回。"""
    if data.startswith("data:"):
        return data
    return f"data:{mime};base64,{data}"


def _mime_from_data_url(data: str) -> str | None:
    """从 data URL 前缀里解析 MIME，例如 data:audio/mp3;base64,xxx。"""
    if not data.startswith("data:") or ":" not in data:
        return None
    header = data[len("data:") :].split(",", 1)[0]
    mime = header.split(";", 1)[0].strip()
    return mime or None


def _audio_mime_from_format(audio_format: str | None) -> str | None:
    """从简写音频格式推断 data URL 需要的 MIME。"""
    if not audio_format:
        return None
    lowered = audio_format.lower()
    if lowered == "mp3":
        return "audio/mpeg"
    if lowered == "wav":
        return "audio/wav"
    return None


def _opt(value: object) -> str | None:
    """把可选字段规范成非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
