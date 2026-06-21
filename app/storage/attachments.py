from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse
from urllib.request import Request, urlopen

from ..event.event import StandardEvent

DOWNLOAD_TIMEOUT_SECONDS = 30.0
TRANSCODE_TIMEOUT_SECONDS = 120.0

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.DOTALL)
_SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a"}
_SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/aac",
    "audio/m4a",
    "audio/mp3",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-m4a",
    "audio/x-wav",
}
_UNSUPPORTED_AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus"}


class AttachmentProcessingError(RuntimeError):
    """Raised when an attachment cannot be materialized and uploaded."""


@dataclass(frozen=True)
class _MaterializedAttachment:
    data: bytes
    filename: str
    mime_type: str | None = None


def process_event_attachments(
    event: StandardEvent,
    *,
    data_dir: Path | str,
    llm: Any,
    poll_interval_seconds: float,
    wait_timeout_seconds: float,
    download_timeout_seconds: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> StandardEvent:
    """Save standard attachment sources locally, upload them, and add file refs."""
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return event

    level = str(event.attention_level or "L0").strip().upper() or "L0"
    next_attachments: list[object] = []
    changed = False
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict) or not _needs_processing(attachment):
            next_attachments.append(attachment)
            continue

        try:
            next_attachments.append(
                _process_attachment(
                    attachment,
                    index=index,
                    data_dir=Path(data_dir),
                    level=level,
                    llm=llm,
                    poll_interval_seconds=poll_interval_seconds,
                    wait_timeout_seconds=wait_timeout_seconds,
                    download_timeout_seconds=download_timeout_seconds,
                )
            )
        except AttachmentProcessingError:
            raise
        except Exception as exc:  # noqa: BLE001 - add attachment index context
            raise AttachmentProcessingError(f"attachments[{index}] 处理失败: {exc}") from exc
        changed = True

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


def _needs_processing(attachment: dict[str, Any]) -> bool:
    if _already_processed(attachment):
        return False
    if _clean_str(attachment.get("file_id")) and not _has_standard_source(attachment):
        return False
    return _has_standard_source(attachment)


def _already_processed(attachment: dict[str, Any]) -> bool:
    return bool(
        _clean_str(attachment.get("file_id"))
        and _clean_str(attachment.get("local_path"))
        and _clean_str(attachment.get("sha256"))
        and attachment.get("size_bytes") is not None
        and _clean_str(attachment.get("upload_status"))
    )


def _has_standard_source(attachment: dict[str, Any]) -> bool:
    return bool(
        _clean_str(attachment.get("url"))
        or _clean_str(attachment.get("data") or attachment.get("base64"))
        or _attachment_path(attachment)
    )


def _process_attachment(
    attachment: dict[str, Any],
    *,
    index: int,
    data_dir: Path,
    level: str,
    llm: Any,
    poll_interval_seconds: float,
    wait_timeout_seconds: float,
    download_timeout_seconds: float,
) -> dict[str, Any]:
    materialized = _materialize_attachment(
        attachment,
        index=index,
        download_timeout_seconds=download_timeout_seconds,
    )
    saved_path = _save_attachment(
        materialized,
        data_dir=data_dir,
        level=level,
        kind=str(attachment.get("kind") or "file"),
    )
    upload_path = saved_path
    upload_mime_type = materialized.mime_type
    upload_filename = materialized.filename
    source_digest = _sha256(saved_path)
    source_size_bytes = saved_path.stat().st_size

    if _needs_audio_transcode(attachment, materialized, saved_path):
        upload_path = _transcode_audio_for_upload(saved_path)
        upload_mime_type = "audio/mp4"
        upload_filename = upload_path.name

    digest = _sha256(upload_path)
    size_bytes = upload_path.stat().st_size

    try:
        uploaded = llm.upload_file(
            upload_path,
            preprocess_configs=_attachment_preprocess_configs(attachment),
            poll_interval_seconds=poll_interval_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - normalize upload failures
        raise AttachmentProcessingError(f"attachments[{index}] 上传失败: {exc}") from exc

    file_id = str(getattr(uploaded, "id", "") or "").strip()
    if not file_id:
        raise AttachmentProcessingError(f"attachments[{index}] 上传响应缺少 file_id")

    next_attachment = dict(attachment)
    next_attachment["local_path"] = str(upload_path)
    next_attachment["size_bytes"] = size_bytes
    next_attachment["sha256"] = digest
    next_attachment["file_id"] = file_id
    next_attachment["upload_status"] = str(getattr(uploaded, "status", "") or "").strip()
    next_attachment["filename"] = upload_filename
    if upload_mime_type:
        next_attachment["mime_type"] = upload_mime_type
    if upload_path != saved_path:
        next_attachment["source_local_path"] = str(saved_path)
        next_attachment["source_size_bytes"] = source_size_bytes
        next_attachment["source_sha256"] = source_digest
        next_attachment["source_filename"] = materialized.filename
        if materialized.mime_type:
            next_attachment["source_mime_type"] = materialized.mime_type
        next_attachment["transcoded"] = True
        next_attachment["transcode_format"] = "m4a"
    return next_attachment


def _materialize_attachment(
    attachment: dict[str, Any],
    *,
    index: int,
    download_timeout_seconds: float,
) -> _MaterializedAttachment:
    data = _clean_str(attachment.get("data") or attachment.get("base64"))
    if data:
        return _materialize_data(attachment, data, index=index)

    path = _attachment_path(attachment)
    if path:
        return _materialize_path(attachment, path, index=index)

    url = _clean_str(attachment.get("url"))
    if url:
        return _materialize_url(
            attachment,
            url,
            index=index,
            download_timeout_seconds=download_timeout_seconds,
        )

    raise AttachmentProcessingError(f"attachments[{index}] 缺少可处理的附件来源")


def _materialize_data(
    attachment: dict[str, Any],
    data: str,
    *,
    index: int,
) -> _MaterializedAttachment:
    mime_type = _clean_str(attachment.get("mime_type")) or _data_url_mime(data)
    try:
        payload = _data_url_payload(data)
    except AttachmentProcessingError as exc:
        raise AttachmentProcessingError(f"attachments[{index}] {exc}") from exc
    try:
        raw = base64.b64decode(_compact_base64(payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentProcessingError(f"attachments[{index}] base64 无效") from exc

    filename = _clean_filename(
        _clean_str(attachment.get("filename") or attachment.get("name"))
        or _default_filename(attachment, mime_type)
    )
    return _MaterializedAttachment(raw, filename, mime_type)


def _materialize_path(
    attachment: dict[str, Any],
    path_text: str,
    *,
    index: int,
) -> _MaterializedAttachment:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise AttachmentProcessingError(f"attachments[{index}].path 文件不存在: {path_text}")

    mime_type = _clean_str(attachment.get("mime_type")) or mimetypes.guess_type(path.name)[0]
    filename = _clean_filename(
        _clean_str(attachment.get("filename") or attachment.get("name")) or path.name
    )
    try:
        return _MaterializedAttachment(path.read_bytes(), filename, mime_type)
    except OSError as exc:
        raise AttachmentProcessingError(f"attachments[{index}].path 读取失败: {path_text}") from exc


def _materialize_url(
    attachment: dict[str, Any],
    url: str,
    *,
    index: int,
    download_timeout_seconds: float,
) -> _MaterializedAttachment:
    request = Request(url, headers={"User-Agent": "Wintermute/0.1"})
    try:
        with urlopen(request, timeout=download_timeout_seconds) as response:
            payload = response.read()
            headers = response.headers
            mime_type = (
                _clean_str(attachment.get("mime_type"))
                or _content_type(headers.get("Content-Type"))
                or mimetypes.guess_type(_url_filename(url) or "")[0]
            )
            filename = _clean_filename(
                _clean_str(attachment.get("filename") or attachment.get("name"))
                or _content_disposition_filename(headers.get("Content-Disposition"))
                or _url_filename(url)
                or _default_filename(attachment, mime_type)
            )
            return _MaterializedAttachment(payload, filename, mime_type)
    except AttachmentProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 - urlopen exposes several failure types
        raise AttachmentProcessingError(f"attachments[{index}].url 下载失败: {url}") from exc


def _save_attachment(
    materialized: _MaterializedAttachment,
    *,
    data_dir: Path,
    level: str,
    kind: str,
) -> Path:
    folder = data_dir / "attachments" / level
    folder.mkdir(parents=True, exist_ok=True)
    suffix = _safe_suffix(materialized.filename, materialized.mime_type, kind)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    target = folder / f"{timestamp}_{uuid.uuid4().hex}{suffix}"
    target.write_bytes(materialized.data)
    return target


def _needs_audio_transcode(
    attachment: dict[str, Any],
    materialized: _MaterializedAttachment,
    path: Path,
) -> bool:
    kind = str(attachment.get("kind") or "").strip().lower()
    mime_type = str(materialized.mime_type or "").strip().lower()
    suffix = path.suffix.lower()
    is_audio = (
        kind == "audio"
        or mime_type.startswith("audio/")
        or suffix in _UNSUPPORTED_AUDIO_EXTENSIONS
    )
    if not is_audio:
        return False
    if mime_type in _SUPPORTED_AUDIO_MIME_TYPES or suffix in _SUPPORTED_AUDIO_EXTENSIONS:
        return False
    return True


def _transcode_audio_for_upload(path: Path) -> Path:
    target = path.with_suffix(".m4a")
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise AttachmentProcessingError(
            "音频格式不受 Files API 支持，且未找到 ffmpeg，无法转码为 m4a"
        )
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(target),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=TRANSCODE_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise AttachmentProcessingError(f"音频转码失败: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AttachmentProcessingError("音频转码超时") from exc
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        if len(stderr) > 500:
            stderr = stderr[-500:]
        raise AttachmentProcessingError(f"音频转码失败: {stderr or result.returncode}")
    if not target.is_file() or target.stat().st_size <= 0:
        raise AttachmentProcessingError("音频转码失败: 未生成有效 m4a 文件")
    return target


def _ffmpeg_executable() -> str | None:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    return imageio_ffmpeg.get_ffmpeg_exe()


def _attachment_path(attachment: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "local_path"):
        value = _clean_str(attachment.get(key))
        if value:
            return value
    return None


def _attachment_preprocess_configs(attachment: dict[str, Any]) -> dict[str, Any] | None:
    value = attachment.get("preprocess_configs")
    return value if isinstance(value, dict) and value else None


def _data_url_mime(data: str) -> str | None:
    match = _DATA_URL_RE.match(data)
    if not match:
        return None
    return _clean_str(match.group(1))


def _data_url_payload(data: str) -> str:
    match = _DATA_URL_RE.match(data)
    if not match:
        return data
    if match.group(2) != ";base64":
        raise AttachmentProcessingError("data URL 必须使用 base64 编码")
    return unquote_to_bytes(match.group(3)).decode("ascii")


def _compact_base64(data: str) -> str:
    return "".join(data.split())


def _content_type(value: str | None) -> str | None:
    if not value:
        return None
    return _clean_str(value.split(";", 1)[0])


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, re.IGNORECASE)
    if not match:
        return None
    return unquote(match.group(1).strip())


def _url_filename(url: str) -> str | None:
    name = PurePosixPath(urlparse(url).path).name
    return unquote(name) if name else None


def _default_filename(attachment: dict[str, Any], mime_type: str | None) -> str:
    kind = str(attachment.get("kind") or "file").strip().lower() or "file"
    return f"{kind}{_extension_for(mime_type, kind)}"


def _clean_filename(filename: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", filename).strip("._")
    return cleaned[:120] or "attachment"


def _safe_suffix(filename: str, mime_type: str | None, kind: str) -> str:
    suffix = Path(filename).suffix
    if suffix and len(suffix) <= 16:
        return suffix
    return _extension_for(mime_type, kind)


def _extension_for(mime_type: str | None, kind: str) -> str:
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed
    return {
        "image": ".png",
        "audio": ".mp3",
        "video": ".mp4",
        "file": ".bin",
    }.get(str(kind).strip().lower(), ".bin")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
