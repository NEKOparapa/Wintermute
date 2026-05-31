from __future__ import annotations

from typing import Any

"""把存储的附件字典映射成 Responses API 的多模态 content part。

prompt 层（重建对话历史）、对话层与事件压缩共用这套映射，保证用户消息与
背景事件里的图片 / 音频 / 视频 / 文件以一致的方式呈现给模型。
本模块只依赖标准库，避免与 prompt / event / memory 等上层产生循环依赖。
"""


def to_user_input_message(text: str, attachments: object) -> dict[str, Any]:
    """构建一条用户 input 消息。无附件时用纯文本，否则用多模态 content 列表。"""
    if not isinstance(attachments, list) or not attachments:
        return {"role": "user", "content": text}

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "input_text", "text": text})
    for attachment in attachments:
        part = to_content_part(attachment)
        if part is not None:
            parts.append(part)
    if not parts:
        parts.append({"type": "input_text", "text": text})
    return {"role": "user", "content": parts}


def to_content_part(attachment: object) -> dict[str, Any] | None:
    """把一条附件字典映射成 Responses API 的 content part；无法表达时返回 None。"""
    if not isinstance(attachment, dict):
        return None

    # content_part 直通：兼容服务需要特殊结构时，直接使用调用方给出的原始 part。
    raw_part = attachment.get("content_part")
    if isinstance(raw_part, dict):
        return dict(raw_part)

    kind = str(attachment.get("kind", "")).strip().lower()
    url = _opt(attachment.get("url"))
    data = _opt(attachment.get("data"))
    file_id = _opt(attachment.get("file_id"))
    mime = _opt(attachment.get("mime_type"))

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
        if url:
            return {"type": "input_video", "video_url": url}
        if file_id:
            return {"type": "input_video", "file_id": file_id}
        if data:
            return {"type": "input_video", "video_url": _data_url(mime or "video/mp4", data)}
        return None

    return None


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
