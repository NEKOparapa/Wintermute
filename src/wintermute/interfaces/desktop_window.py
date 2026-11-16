"""Accept a completed desktop dwell and recognize its representative frames in memory."""

import asyncio
import base64
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from aiohttp import web
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator

from wintermute.config import HTTPTerminalSettings
from wintermute.events import DuplicateEventError, EventError, EventGateway
from wintermute.events.core.types import Identifier
from wintermute.events.slices.image import MAX_IMAGE_BYTES, ImageSliceProcessor, ScreenshotReference
from wintermute.interfaces.image import validate_jpeg
from wintermute.interfaces.media_file import desktop_frame_directory, save_media

_MAX_BASE64 = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
_MAX_REQUEST = 4 * _MAX_BASE64 + 256 * 1024
DIGEST_PREFIX = "desktop-window:"


def _iso_time(value: str) -> str:
    if datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("Timezone required")
    return value


type _Time = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_iso_time)]
type _Title = Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _WorkObject(_Input):
    occurred_at: _Time
    window_title: _Title


class _Span(_Input):
    foreground_app: Annotated[Identifier, Field(pattern=r"\S")]
    started_at: _Time
    ended_at: _Time
    duration_seconds: Annotated[int, Field(ge=0)]
    window_title: _Title | None = None
    work_objects: Annotated[list[_WorkObject], Field(max_length=16)] | None = None
    work_objects_omitted: Annotated[int, Field(ge=1)] | None = None


class _Frame(_Input):
    occurred_at: _Time
    window_title: _Title | None = None
    jpeg_base64: Annotated[str, Field(min_length=4, max_length=_MAX_BASE64)]


class _Window(_Input):
    event_id: Annotated[Identifier, Field(pattern=r"\S")]
    occurred_at: _Time
    state_key: Literal["desktop.window_span"]
    value: _Span
    frames: Annotated[list[_Frame], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def check_times(self) -> Self:
        start = datetime.fromisoformat(self.value.started_at)
        end = datetime.fromisoformat(self.value.ended_at)
        if (
            end < start
            or self.value.duration_seconds > (end - start).total_seconds() + 0.5
            or datetime.fromisoformat(self.occurred_at) != end
        ):
            raise ValueError("Invalid span duration")
        for observations in (self.frames, self.value.work_objects or []):
            last = start
            for item in observations:
                at = datetime.fromisoformat(item.occurred_at)
                if not last <= at <= end:
                    raise ValueError("Observations must be ordered within the dwell")
                last = at
        return self


def _parse(raw: bytes) -> tuple[_Window, str]:
    span = _Window.model_validate_json(raw)
    # The original request fingerprint is independent of the model's generated wording.
    fingerprint = hashlib.sha256(span.model_dump_json(exclude_none=True).encode()).hexdigest()
    return span, DIGEST_PREFIX + fingerprint


def _validate_images(span: _Window) -> None:
    for frame in span.frames:
        data = base64.b64decode(frame.jpeg_base64, validate=True)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image too large")
        validate_jpeg(data)


def _save_frames(directory: Path, span: _Window) -> list[dict[str, str]]:
    if directory.resolve() != directory:
        raise OSError("Invalid frame directory")
    frames = []
    for frame in span.frames:
        data = base64.b64decode(frame.jpeg_base64, validate=True)
        digest = save_media(directory, data, ".jpg")
        metadata = {"sha256": digest, "occurred_at": frame.occurred_at}
        if frame.window_title:
            metadata["window_title"] = frame.window_title
        frames.append(metadata)
    return frames


class DesktopWindowUploads:
    def __init__(
        self, gateway: EventGateway, vision: ImageSliceProcessor | None, directory: Path
    ) -> None:
        self.gateway = gateway
        self.vision = vision
        self.directory = directory
        self.slots = asyncio.Semaphore(2)
        self.pending: set[tuple[str, str]] = set()

    async def _finish(self, user: str, terminal: str, producer: str, fingerprint: str) -> None:
        record = self.gateway.store.received_event(terminal, producer)
        if (
            record is None or record.user_id != user or record.correlation_id != fingerprint
            or not record.payload.get("frames")
        ):
            directory = desktop_frame_directory(self.directory, user, terminal, fingerprint)
            if directory.resolve() == directory and directory.is_dir():
                await asyncio.to_thread(shutil.rmtree, directory)
        self.gateway.store.finish_frame_upload(terminal, producer, fingerprint)

    async def recover(self, _app: web.Application) -> None:
        # The service holds the scheduler lease and has not opened the listening socket yet.
        for upload in self.gateway.store.pending_frame_uploads():
            await self._finish(*upload)

    def _receipt(
        self, binding: HTTPTerminalSettings, span: _Window, fingerprint: str
    ) -> web.Response | None:
        old = self.gateway.store.received_event(binding.terminal_id, span.event_id)
        if old is None:
            return None
        if (
            old.layer == "perception"
            and old.user_id == binding.user_id
            and old.conversation_id == binding.conversation_id
            and old.correlation_id == fingerprint
        ):
            return web.json_response({"event_id": old.event_id, "duplicate": True}, status=202)
        return web.json_response({"error": "event_id_conflict"}, status=409)

    async def submit(self, request: web.Request, binding: HTTPTerminalSettings) -> web.Response:
        if request.content_type != "application/json" or request.headers.get("Content-Encoding"):
            return web.json_response({"error": "json_required"}, status=415)
        if (request.content_length or 0) > _MAX_REQUEST:
            return web.json_response({"error": "payload_too_large"}, status=413)
        if self.slots.locked():
            return web.json_response({"error": "desktop_vision_busy"}, status=429)
        async with self.slots:
            try:
                async with asyncio.timeout(60):
                    raw = await request.clone(client_max_size=_MAX_REQUEST).read()
                span, fingerprint = await asyncio.to_thread(_parse, raw)
                del raw
            except TimeoutError:
                return web.json_response({"error": "upload_timeout"}, status=408)
            except (ValueError, OverflowError):
                return web.json_response({"error": "invalid_window_observation"}, status=400)
            receipt = self._receipt(binding, span, fingerprint)
            if receipt is not None:
                return receipt
            if self.vision is None:
                return web.json_response({"error": "desktop_vision_disabled"}, status=503)
            key = (binding.terminal_id, span.event_id)
            if key in self.pending:
                return web.json_response({"error": "desktop_vision_busy"}, status=429)
            self.pending.add(key)
            try:
                return await self._recognize(binding, span, fingerprint, self.vision)
            finally:
                self.pending.remove(key)

    async def _recognize(
        self,
        binding: HTTPTerminalSettings,
        span: _Window,
        fingerprint: str,
        vision: ImageSliceProcessor,
    ) -> web.Response:
        try:
            await asyncio.to_thread(_validate_images, span)
        except ValueError:
            return web.json_response({"error": "invalid_window_observation"}, status=400)
        observations: list[JsonValue] = []
        previous: ScreenshotReference | None = None
        last_title = None
        for frame in span.frames:
            if frame.window_title != last_title:
                previous = None
            try:
                obs = await vision.recognize(
                    "data:image/jpeg;base64," + frame.jpeg_base64,
                    span.value.foreground_app,
                    frame.window_title,
                    previous,
                )
            except Exception:
                # Nothing is committed when a frame fails; the desktop retains the full request.
                return web.json_response({"error": "desktop_vision_unavailable"}, status=503)
            if obs.facts:
                text = "；".join(obs.facts)
                if obs.subject and obs.subject != frame.window_title:
                    text = f"{obs.subject}：{text}"
                record: dict[str, JsonValue] = {"occurred_at": frame.occurred_at, "text": text}
                if frame.window_title:
                    record["window_title"] = frame.window_title
                observations.append(record)
            keep = previous if not obs.changed_subject else None
            previous = ScreenshotReference(
                "",
                obs.subject,
                (*(keep.facts if keep else ()), *obs.facts),
                evidence_keys=(*(keep.evidence_keys if keep else ()), *obs.evidence_keys),
            )
            last_title = frame.window_title
        value = span.value.model_dump(exclude_none=True)
        value["visual_observations"] = observations
        endpoint = self.gateway.perception.connect(
            binding.terminal_id, conversation_id=binding.conversation_id
        )
        directory = desktop_frame_directory(
            self.directory, binding.user_id, binding.terminal_id, fingerprint
        )
        upload = (binding.user_id, binding.terminal_id, span.event_id, fingerprint)
        if not self.gateway.store.begin_frame_upload(*upload):
            return web.json_response({"error": "desktop_vision_busy"}, status=429)
        try:
            # Let any in-flight disk write finish before cancellation cleans up the directory.
            writing = asyncio.create_task(asyncio.to_thread(_save_frames, directory, span))
            try:
                metadata = await asyncio.shield(writing)
            except asyncio.CancelledError:
                await writing
                raise
            receipt = await endpoint.publish(
                "state.observed",
                {
                    "subject": f"device:{binding.terminal_id}",
                    "state_key": span.state_key,
                    "value": value,
                    "frames": metadata,
                },
                producer_event_id=span.event_id,
                occurred_at=datetime.fromisoformat(span.occurred_at),
                correlation_id=fingerprint,
            )
        except DuplicateEventError:
            raced = self._receipt(binding, span, fingerprint)
            if raced is not None:
                return raced
            return web.json_response({"error": "event_id_conflict"}, status=409)
        except (EventError, OverflowError):
            return web.json_response({"error": "event_rejected"}, status=400)
        except OSError:
            return web.json_response({"error": "screenshot_storage_unavailable"}, status=503)
        finally:
            await self._finish(*upload)
        return web.json_response(
            {"event_id": receipt.event.event_id, "duplicate": receipt.duplicate}, status=202
        )
