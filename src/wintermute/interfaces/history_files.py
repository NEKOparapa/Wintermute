"""File metadata and authenticated downloads for media stored with slice events."""

import re
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from wintermute.config.models import HTTPTerminalSettings
from wintermute.events import EventError, EventRecord, SQLiteStore
from wintermute.events.core.types import AudioSliceData


@dataclass(frozen=True)
class _Attachment:
    path: Path
    name: str
    content_type: str


class HistoryFiles:
    def __init__(self, image_directory: Path, audio_directory: Path) -> None:
        self.image_directory = image_directory.resolve()
        self.audio_directory = audio_directory.resolve()

    def _attachment(self, event: EventRecord) -> _Attachment | None:
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
        try:
            if path.resolve(strict=True) != path or not path.is_file():
                return None
        except (OSError, RuntimeError):
            return None
        return _Attachment(path, name, content_type)

    def describe(self, event: EventRecord) -> list[dict[str, str]]:
        attachment = self._attachment(event)
        return [{"id": event.event_id, "name": attachment.name}] if attachment else []

    def download(
        self, store: SQLiteStore, binding: HTTPTerminalSettings, event_id: str
    ) -> web.FileResponse:
        try:
            event = store.get_event(event_id, user_id=binding.user_id)
        except EventError:
            raise web.HTTPNotFound from None
        if event.conversation_id not in (None, binding.conversation_id):
            raise web.HTTPNotFound
        attachment = self._attachment(event)
        if attachment is None:
            raise web.HTTPNotFound
        return web.FileResponse(
            attachment.path,
            headers={
                "Content-Type": attachment.content_type,
                "Content-Disposition": f'attachment; filename="{attachment.name}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
