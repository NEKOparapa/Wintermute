from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 事件层支持的多模态附件类型。具体可用类型取决于所配置的兼容服务端和模型。
SUPPORTED_ATTACHMENT_KINDS = {"image", "audio", "video", "file"}


@dataclass(frozen=True)
class Attachment:
    """一条多模态附件，承载图片 / 音频 / 视频 / 文件输入。

    至少要提供 url、data、file_id、path、content_part 其中之一，否则视为无效附件。
    content_part 是直通逃生舱：当某个兼容服务需要特殊的 content part 结构时，
    可以直接给出原始的 Responses content part 字典，绕过内置映射。
    """

    kind: str
    url: str | None = None
    data: str | None = None  # base64 原文（不含 data: 前缀）
    mime_type: str | None = None
    format: str | None = None  # 音频格式：mp3 / wav，用于 data URL MIME 推断
    filename: str | None = None
    detail: str | None = None  # 图片细节，具体取值由服务端决定
    file_id: str | None = None
    path: str | None = None  # 本地文件路径，进入 LLM 前会先上传成 file_id
    preprocess_configs: dict[str, Any] | None = None
    content_part: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成可写入事件 metadata 的纯 JSON 字典，省略空字段。"""
        data: dict[str, Any] = {"kind": self.kind}
        for key in (
            "url",
            "data",
            "mime_type",
            "format",
            "filename",
            "detail",
            "file_id",
            "path",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.preprocess_configs:
            data["preprocess_configs"] = self.preprocess_configs
        if self.content_part:
            data["content_part"] = self.content_part
        return data


def normalize_event(
    message: str | None = None,
    attachments: Any = None,
    *,
    source: str = "user",
    type: str = "user_message",
    attention_level: str = "L0",
    metadata: dict[str, Any] | None = None,
) -> StandardEvent:
    """把外部输入归一化成标准事件，支持自定义源头、类型与注意力等级。

    L0/L1 会话事件通常用默认的 source=user / type=user_message；
    L2/L3 背景事件由调用方给出来源（如 sensor:door）和 type=observation。
    """
    text = (message or "").strip()
    parsed = normalize_attachments(attachments)
    if not text and not parsed:
        raise ValueError("message 与 attachments 不能同时为空。")

    event_metadata: dict[str, Any] = dict(metadata or {})
    if parsed:
        event_metadata["attachments"] = [item.to_dict() for item in parsed]

    return StandardEvent(
        source=source,
        type=type,
        content=text,
        metadata=event_metadata,
        attention_level=attention_level,
    )


def normalize_message_event(
    message: str | None = None,
    attachments: Any = None,
) -> StandardEvent:
    """把外部输入消息（可带多模态附件）归一化成 L0 用户消息事件。"""
    return normalize_event(
        message,
        attachments,
        source="user",
        type="user_message",
        attention_level="L0",
    )


def normalize_attachments(raw: Any) -> list[Attachment]:
    """把外部传入的附件列表解析成 Attachment 列表，非法项直接报错。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments 必须是数组。")
    return [_normalize_attachment(item, index) for index, item in enumerate(raw)]


@dataclass(frozen=True)
class StandardEvent:
    """事件层输出的标准事件格式。"""

    source: str
    type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attention_level: str = "L0"


def _normalize_attachment(raw: Any, index: int) -> Attachment:
    """解析并校验单条附件；kind 必填且必须提供一种内容来源。"""
    if not isinstance(raw, dict):
        raise ValueError(f"attachments[{index}] 必须是对象。")

    kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
    if kind not in SUPPORTED_ATTACHMENT_KINDS:
        raise ValueError(
            f"attachments[{index}].kind 无效: {kind or '(空)'}，"
            f"支持 {sorted(SUPPORTED_ATTACHMENT_KINDS)}。"
        )

    content_part = raw.get("content_part")
    if content_part is not None and not isinstance(content_part, dict):
        raise ValueError(f"attachments[{index}].content_part 必须是对象。")
    preprocess_configs = raw.get("preprocess_configs")
    if preprocess_configs is not None and not isinstance(preprocess_configs, dict):
        raise ValueError(f"attachments[{index}].preprocess_configs 必须是对象。")

    attachment = Attachment(
        kind=kind,
        url=_clean_str(raw.get("url")),
        data=_clean_str(raw.get("data") or raw.get("base64")),
        mime_type=_clean_str(raw.get("mime_type") or raw.get("mime")),
        format=_clean_str(raw.get("format")),
        filename=_clean_str(raw.get("filename") or raw.get("name")),
        detail=_clean_str(raw.get("detail")),
        file_id=_clean_str(raw.get("file_id")),
        path=_clean_str(raw.get("path") or raw.get("file_path") or raw.get("local_path")),
        preprocess_configs=preprocess_configs,
        content_part=content_part,
    )

    if not (
        attachment.url
        or attachment.data
        or attachment.file_id
        or attachment.path
        or attachment.content_part
    ):
        raise ValueError(
            f"attachments[{index}] 需要提供 url、data、file_id、path 或 content_part 之一。"
        )
    return attachment


def _clean_str(value: Any) -> str | None:
    """把可选字段规范成非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
