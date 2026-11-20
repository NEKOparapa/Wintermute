"""File metadata and authenticated downloads for perception and slice events."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from wintermute.config.models import HTTPDeviceBinding
from wintermute.events.core import EventError, EventRecord
from wintermute.events.core.attachments import HistoryAttachment
from wintermute.events.core.types import AudioSliceData
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.interfaces.media_file import desktop_frame_directory


def _stored_file(path: Path) -> bool:
    try:
        return path.resolve(strict=True) == path and path.is_file()
    except (OSError, RuntimeError):
        return False


class HistoryFiles:
    def __init__(self, image_directory: Path, audio_directory: Path) -> None:
        self.image_directory = image_directory.resolve()
        self.audio_directory = audio_directory.resolve()

    def _attachment(self, event: EventRecord) -> HistoryAttachment | None:
        if event.layer != "slice":
            return None
        timestamp = event.occurred_at.strftime("%Y%m%dT%H%M%SZ")
        if event.type == "slice.image":
            digest = event.payload.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                return None
            path = self.image_directory / (digest + ".jpg")
            name = f"screenshot-{timestamp}-{digest[:8]}.jpg"
            content_type = "image/jpeg"
        elif event.type == "slice.audio":
            audio = AudioSliceData.model_validate(event.payload["audio"])
            path = self.audio_directory / (audio.sha256 + ".ogg")
            name = f"recording-{timestamp}-{audio.sequence}-{audio.sha256[:8]}.ogg"
            content_type = "audio/ogg"
        else:
            return None
        if not _stored_file(path):
            return None
        return HistoryAttachment(path, name, content_type)

    def _attachments(self, event: EventRecord) -> Iterator[tuple[str, HistoryAttachment]]:
        if event.layer != "perception":
            attachment = self._attachment(event)
            if attachment:
                yield event.event_id, attachment
            return
        fingerprint = event.correlation_id or ""
        frames = event.payload.get("frames")
        if (
            event.type != "state.observed"
            or event.payload.get("state_key") != "desktop.window_span"
            or not re.fullmatch(r"desktop-window:[0-9a-f]{64}", fingerprint)
            or not isinstance(frames, list)
        ):
            return
        directory = desktop_frame_directory(
            self.image_directory, event.user_id, event.device_id, fingerprint
        )
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue
            digest, when = frame.get("sha256"), frame.get("occurred_at")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            if not isinstance(when, str):
                continue
            try:
                timestamp = datetime.fromisoformat(when).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            except (ValueError, OverflowError):
                continue
            path = directory / (digest + ".jpg")
            if _stored_file(path):
                title = frame.get("window_title")
                yield (
                    f"{event.event_id}:frame:{index}",
                    HistoryAttachment(
                        path,
                        f"screenshot-{timestamp}-{index + 1}-{digest[:8]}.jpg",
                        "image/jpeg",
                        when,
                        title if isinstance(title, str) else None,
                    ),
                )

    def describe(self, event: EventRecord) -> list[dict[str, str]]:
        images = event.payload.get("image_hashes")
        if event.layer == "conversation":
            names = event.payload.get("image_names")
            files = [
                {
                    "id": f"{event.event_id}:chat:{i}",
                    "name": str(names[i])
                    if isinstance(names, list) and i < len(names)
                    else f"聊天图片-{i + 1}.jpg",
                    "content_type": "image/jpeg",
                }
                for i in range(len(images) if isinstance(images, list) else 0)
            ]
            docs = event.payload.get("documents")
            if isinstance(docs, list):
                files.extend(
                    {
                        "id": str(doc["id"]),
                        "name": str(doc["name"]),
                        "content_type": "application/octet-stream",
                    }
                    for doc in docs
                    if isinstance(doc, dict)
                )
            return files
        files = []
        for id_, attachment in self._attachments(event):
            item = {"id": id_, "name": attachment.name}
            if attachment.occurred_at is not None:
                item.update(
                    content_type=attachment.content_type, occurred_at=attachment.occurred_at
                )
                if attachment.window_title:
                    item["window_title"] = attachment.window_title
            files.append(item)
        return files

    def resolve(
        self, store: SQLiteStore, user_id: str, conversation_id: str, event_id: str
    ) -> HistoryAttachment:
        owner_id = event_id.partition(":frame:")[0]
        try:
            event = store.get_event(owner_id, user_id=user_id)
        except EventError:
            raise LookupError("此会话没有该附件") from None
        if event.conversation_id not in (None, conversation_id):
            raise LookupError("此会话没有该附件")
        for id_, attachment in self._attachments(event):
            if id_ == event_id:
                return attachment
        raise LookupError("此会话没有该附件")

    def download(
        self, store: SQLiteStore, binding: HTTPDeviceBinding, event_id: str
    ) -> web.FileResponse:
        try:
            attachment = self.resolve(store, binding.user_id, binding.conversation_id, event_id)
        except LookupError:
            raise web.HTTPNotFound from None
        return web.FileResponse(
            attachment.path,
            headers={
                "Content-Type": attachment.content_type,
                "Content-Disposition": f'attachment; filename="{attachment.name}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
