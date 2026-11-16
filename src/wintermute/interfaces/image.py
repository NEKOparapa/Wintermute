"""Authenticated intake for manual and periodic screenshots."""

import asyncio
import base64
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

from aiohttp import web
from PIL import Image
from pydantic import JsonValue

from wintermute.config.models import HTTPTerminalSettings, ImageSliceSettings
from wintermute.events import DuplicateEventError, EventError, EventGateway
from wintermute.events.core.types import ScreenshotContext
from wintermute.events.slices.image import MAX_IMAGE_BYTES
from wintermute.interfaces.media_file import save_media


def validate_jpeg(data: bytes) -> None:
    try:
        with Image.open(BytesIO(data), formats=["JPEG"]) as image:
            if image.width * image.height > 12_000_000:
                raise ValueError("Screenshot too large")
            image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid JPEG") from exc


def _store_jpeg(directory: Path, data: bytes) -> str:
    validate_jpeg(data)
    return save_media(directory, data, ".jpg")


class ImageUploads:
    def __init__(self, settings: ImageSliceSettings, gateway: EventGateway, base_dir: str | Path):
        self.enabled = settings.enabled
        self.directory = (Path(base_dir) / Path(settings.storage_path).expanduser()).resolve()
        self.gateway = gateway
        self.slots = asyncio.Semaphore(2)
        self.commit_lock = asyncio.Lock()

    async def submit(self, request: web.Request, binding: HTTPTerminalSettings) -> web.Response:
        if not self.enabled:
            return web.json_response({"error": "image_disabled"}, status=503)
        if request.content_type != "image/jpeg" or request.headers.get("Content-Encoding"):
            return web.json_response({"error": "jpeg_required"}, status=415)
        try:
            ids = request.headers.getall("X-Event-ID", [])
            times = request.headers.getall("X-Occurred-At", [])
            if len(ids) != 1 or len(times) != 1:
                raise ValueError
            event_id = "screenshot:" + str(UUID(ids[0]))
            occurred_at = datetime.fromisoformat(times[0])
            if occurred_at.utcoffset() is None:
                raise ValueError
            occurred_at = occurred_at.astimezone(UTC)
            contexts = request.headers.getall("X-Capture-Context", [])
            capture = None
            if contexts:
                if len(contexts) != 1 or len(contexts[0]) > 8192:
                    raise ValueError
                encoded = contexts[0]
                raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), b"-_", validate=True)
                capture = ScreenshotContext.model_validate_json(raw)
                if capture.start is not None and capture.start > occurred_at.timestamp() * 1000 + 1:
                    raise ValueError
        except (ValueError, OverflowError):
            return web.json_response({"error": "invalid_image_metadata"}, status=400)
        if (request.content_length or 0) > MAX_IMAGE_BYTES:
            return web.json_response({"error": "image_too_large"}, status=413)
        if self.slots.locked():
            return web.json_response({"error": "image_upload_busy"}, status=429)
        async with self.slots:
            body = bytearray()
            try:
                async with asyncio.timeout(60):
                    async for chunk in request.content.iter_chunked(65536):
                        if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                            return web.json_response({"error": "image_too_large"}, status=413)
                        body.extend(chunk)
            except TimeoutError:
                return web.json_response({"error": "image_upload_timeout"}, status=408)
            async with self.commit_lock:
                return await self._commit(bytes(body), binding, event_id, occurred_at, capture)

    async def _commit(
        self,
        body: bytes,
        binding: HTTPTerminalSettings,
        event_id: str,
        occurred_at: datetime,
        capture: ScreenshotContext | None = None,
    ) -> web.Response:
        payload: dict[str, JsonValue] = {"sha256": hashlib.sha256(body).hexdigest()}
        if capture is not None:
            payload["capture"] = capture.model_dump(exclude_none=True)
        previous = self.gateway.store.received_event(binding.terminal_id, event_id)
        if previous is not None and (
            previous.type != "slice.image"
            or previous.user_id != binding.user_id
            or previous.conversation_id != binding.conversation_id
            or previous.payload != payload
            or previous.occurred_at != occurred_at
        ):
            return web.json_response({"error": "event_id_conflict"}, status=409)
        if capture and capture.platform in ("windows", "macos"):
            if previous is not None:
                return web.json_response(
                    {"event_id": previous.event_id, "duplicate": True}, status=202
                )
            return web.json_response({"error": "desktop_window_perception_required"}, status=422)
        try:
            await asyncio.to_thread(_store_jpeg, self.directory, body)
        except ValueError:
            return web.json_response({"error": "invalid_screenshot"}, status=400)
        endpoint = self.gateway.slice.connect(
            binding.terminal_id,
            conversation_id=binding.conversation_id,
            source_id="app_screenshot" if capture else "screenshot",
        )
        try:
            receipt = await endpoint.publish(
                "slice.image",
                payload,
                producer_event_id=event_id,
                correlation_id=capture.activity_id if capture else None,
                occurred_at=occurred_at,
            )
        except DuplicateEventError:
            return web.json_response({"error": "event_id_conflict"}, status=409)
        except (EventError, OverflowError):
            return web.json_response({"error": "event_rejected"}, status=400)
        return web.json_response(
            {"event_id": receipt.event.event_id, "duplicate": receipt.duplicate}, status=202
        )
