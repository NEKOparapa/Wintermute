from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .event import StandardEvent

logger = logging.getLogger(__name__)


def upload_local_attachments(
    event: StandardEvent,
    llm: Any,
    *,
    poll_interval_seconds: float,
    wait_timeout_seconds: float,
) -> StandardEvent:
    """把 metadata.attachments 中的本地 path 上传为 file_id，返回替换后的事件。

    对话事件（L0/L1）与背景事件（L2/L3）共用：本地路径是临时的，先上传成 file_id
    再落库，既让后续多模态压缩 / 对话能访问到媒体，也避免事件流里残留易失效的本地路径。
    """
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return event

    next_attachments: list[object] = []
    changed = False
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            next_attachments.append(attachment)
            continue

        local_path = _local_attachment_path(attachment)
        next_attachment = dict(attachment)
        if not local_path:
            next_attachments.append(next_attachment)
            continue

        changed = True
        for key in ("path", "file_path", "local_path"):
            next_attachment.pop(key, None)

        if next_attachment.get("file_id"):
            next_attachments.append(next_attachment)
            continue

        resolved_path = Path(local_path).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = Path.cwd() / resolved_path
        if not resolved_path.is_file():
            raise ValueError(f"attachments[{index}].path 文件不存在: {local_path}")

        logger.info("上传本地附件 path=%s kind=%s", resolved_path, next_attachment.get("kind"))
        uploaded = llm.upload_file(
            resolved_path,
            preprocess_configs=_attachment_preprocess_configs(next_attachment),
            poll_interval_seconds=poll_interval_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        next_attachment["file_id"] = uploaded.id
        next_attachment.setdefault("filename", resolved_path.name)
        next_attachment.pop("preprocess_configs", None)
        next_attachments.append(next_attachment)

    if not changed:
        return event

    next_metadata = dict(metadata)
    next_metadata["attachments"] = next_attachments
    return StandardEvent(
        source=event.source,
        type=event.type,
        content=event.content,
        metadata=next_metadata,
        attention_level=event.attention_level,
    )


def _local_attachment_path(attachment: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "local_path"):
        value = attachment.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


def _attachment_preprocess_configs(attachment: dict[str, Any]) -> dict[str, Any] | None:
    value = attachment.get("preprocess_configs")
    return value if isinstance(value, dict) and value else None
