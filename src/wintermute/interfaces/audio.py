"""Authenticated device audio ingestion; immutable files precede durable slice receipts."""

import asyncio
import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web

from wintermute.config.models import AudioSliceSettings, HTTPTerminalSettings
from wintermute.events import DuplicateEventError, EventError, EventGateway


class AudioUploadError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _crc_table() -> tuple[int, ...]:
    entries = []
    for value in range(256):
        value <<= 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
        entries.append(value)
    return tuple(entries)


_OGG_CRC = _crc_table()


def _validate_tags(body: bytes, lacing: bytes) -> None:
    if len(body) < 16 or not body.startswith(b"OpusTags") or sum(x < 255 for x in lacing) != 1:
        raise ValueError("Expected one complete OpusTags packet")
    position = 12 + int.from_bytes(body[8:12], "little")
    if position + 4 > len(body):
        raise ValueError("Incomplete OpusTags vendor")
    count = int.from_bytes(body[position : position + 4], "little")
    position += 4
    if count > (len(body) - position) // 4:
        raise ValueError("Incomplete OpusTags comments")
    for _ in range(count):
        if position + 4 > len(body):
            raise ValueError("Incomplete OpusTags comment length")
        position += 4 + int.from_bytes(body[position : position + 4], "little")
        if position > len(body):
            raise ValueError("Incomplete OpusTags comment")


def validate_recording(data: bytes, samples: int) -> None:
    """Check the firmware's single-stream, complete-page Ogg/Opus segment format.

    Granules use 48 kHz even though the capture rate is 16 kHz. Checking EOS,
    CRC, page order and pre-skip prevents partial files or incorrect duration
    metadata from becoming successfully accepted recordings.
    """
    offset = page_number = 0
    serial: bytes | None = None
    pre_skip = last_granule = 0
    ended = False
    while offset < len(data):
        header = data[offset : offset + 27]
        if len(header) != 27 or header[:5] != b"OggS\x00" or ended:
            raise ValueError("Invalid Ogg page")
        flags, segments = header[5], header[26]
        if flags not in (0, 2, 4) or not segments:
            raise ValueError("Unsupported Ogg page flags")
        lacing = data[offset + 27 : offset + 27 + segments]
        if len(lacing) != segments or lacing[-1] == 255:
            raise ValueError("Incomplete Ogg packet")
        body_start = offset + 27 + segments
        end = body_start + sum(lacing)
        if end > len(data) or int.from_bytes(header[18:22], "little") != page_number:
            raise ValueError("Incomplete or unordered Ogg page")
        page = bytearray(data[offset:end])
        checksum = int.from_bytes(page[22:26], "little")
        page[22:26] = b"\x00" * 4
        crc = 0
        for value in page:
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC[(crc >> 24) ^ value]
        if crc != checksum:
            raise ValueError("Ogg checksum mismatch")
        granule = int.from_bytes(header[6:14], "little")
        body = data[body_start:end]
        if page_number == 0:
            if (
                flags != 2
                or lacing != b"\x13"
                or body[:10] != b"OpusHead\x01\x01"
                or int.from_bytes(body[12:16], "little") != 16000
                or body[18] != 0
                or granule != 0
            ):
                raise ValueError("Expected mono 16 kHz OpusHead")
            serial = header[14:18]
            pre_skip = int.from_bytes(body[10:12], "little")
        elif header[14:18] != serial or flags == 2:
            raise ValueError("Multiple Ogg streams are unsupported")
        elif page_number == 1:
            if granule != 0 or flags != 0:
                raise ValueError("Expected OpusTags")
            _validate_tags(body, lacing)
        elif granule < last_granule:
            raise ValueError("Ogg granules are unordered")
        if page_number >= 2:
            packet_bytes = 0
            for lace in lacing:
                packet_bytes += lace
                if lace < 255:
                    if not packet_bytes:
                        raise ValueError("Empty Opus audio packet")
                    packet_bytes = 0
        ended = flags == 4
        last_granule = granule
        offset = end
        page_number += 1
    if not ended or page_number < 3 or last_granule - pre_skip != samples * 3:
        raise ValueError("Missing EOS or incorrect audio duration")


def _save_recording(directory: Path, data: bytes, samples: int) -> str:
    validate_recording(data, samples)
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + ".ogg")

    def intact() -> bool:
        try:
            with target.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest() == digest
        except FileNotFoundError:
            return False

    # On Windows an ASR reader may hold the destination open. Identical retries
    # need no replacement and must still return the original receipt.
    if intact():
        return digest
    # All writers use content-addressed, immutable names; atomic replacement also
    # repairs a damaged local copy when the device retries the original segment.
    descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, target)
        except PermissionError:
            if not intact():
                raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return digest


class AudioUploads:
    def __init__(
        self, settings: AudioSliceSettings, gateway: EventGateway, *, base_dir: str | Path
    ) -> None:
        self.settings = settings.model_copy(deep=True)
        self.gateway = gateway
        path = Path(settings.storage_path).expanduser()
        self.directory = (Path(base_dir).resolve() / path).resolve()
        self._slots = asyncio.Semaphore(2)

    @staticmethod
    def _header(request: web.Request, name: str) -> str:
        values = request.headers.getall(name, [])
        if len(values) != 1:
            raise AudioUploadError(400, "invalid_audio_metadata")
        return values[0]

    @classmethod
    def _number(cls, request: web.Request, name: str, low: int, high: int) -> int:
        value = cls._header(request, name)
        if re.fullmatch(r"[0-9]{1,16}", value) is None or not low <= int(value) <= high:
            raise AudioUploadError(400, "invalid_audio_metadata")
        return int(value)

    async def submit(self, request: web.Request, binding: HTTPTerminalSettings) -> web.Response:
        if not self.settings.enabled:
            raise AudioUploadError(503, "audio_disabled")
        if request.content_type != "audio/ogg" or request.headers.get("Content-Encoding"):
            raise AudioUploadError(415, "ogg_required")
        recording = self._header(request, "X-Recording-ID")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", recording) is None:
            raise AudioUploadError(400, "invalid_audio_metadata")
        sequence = self._number(request, "X-Segment-Sequence", 1, 1_000_000)
        rate = self._number(request, "X-Sample-Rate", 16000, 16000)
        start = self._number(request, "X-Start-Sample", 0, 120 * 60 * rate - 1)
        samples = self._number(request, "X-Sample-Count", 1, 300 * rate)
        window = 120
        if "X-Window-Minutes" in request.headers:
            window = self._number(request, "X-Window-Minutes", 15, 120)
            if window not in (15, 30, 60, 120):
                raise AudioUploadError(400, "invalid_audio_metadata")
        if start + samples > window * 60 * rate:
            raise AudioUploadError(400, "invalid_audio_metadata")
        elapsed = start + samples
        if "X-Recording-Elapsed-Samples" in request.headers:
            elapsed = self._number(
                request, "X-Recording-Elapsed-Samples", start + samples, 365 * 86400 * rate
            )
        occurred_at = datetime.now(UTC) - timedelta(seconds=(elapsed - start) / rate)
        limit = self.settings.max_segment_bytes
        if request.content_length is not None and request.content_length > limit:
            raise AudioUploadError(413, "audio_too_large")
        if self._slots.locked():
            raise AudioUploadError(429, "audio_upload_busy")
        async with self._slots:
            body = bytearray()
            try:
                async with asyncio.timeout(120):
                    async for chunk in request.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > limit:
                            raise AudioUploadError(413, "audio_too_large")
                        body.extend(chunk)
            except TimeoutError:
                raise AudioUploadError(408, "audio_upload_timeout") from None
            try:
                digest = await asyncio.to_thread(
                    _save_recording, self.directory, bytes(body), samples
                )
            except ValueError:
                raise AudioUploadError(400, "invalid_ogg_recording") from None
            audio = {
                "file": digest + ".ogg",
                "sha256": digest,
                "bytes": len(body),
                "format": "ogg",
                "codec": "opus",
                "sample_rate": rate,
                "channels": 1,
                "recording_id": recording,
                "sequence": sequence,
                "start_sample": start,
                "samples": samples,
            }
            try:
                receipt = await self.gateway.slice.connect(
                    binding.terminal_id,
                    conversation_id=binding.conversation_id,
                    source_id="recording:" + recording,
                ).record_audio(
                    audio,
                    producer_event_id=f"audio:{recording}:{sequence}",
                    occurred_at=occurred_at,
                    source_seq=sequence,
                )
            except DuplicateEventError:
                raise AudioUploadError(409, "audio_id_conflict") from None
            except EventError:
                raise AudioUploadError(400, "audio_event_rejected") from None
        status = self.gateway.store.get_slice_status(receipt.event.event_id)
        return web.json_response(
            {
                "event_id": receipt.event.event_id,
                "duplicate": receipt.duplicate,
                "status": status.status,
            },
            status=202,
        )

    async def status(self, request: web.Request, binding: HTTPTerminalSettings) -> web.Response:
        event_id = request.match_info["event_id"]
        try:
            event = self.gateway.store.get_event(event_id, user_id=binding.user_id)
        except EventError:
            raise AudioUploadError(404, "not_found") from None
        if (
            event.type != "slice.audio"
            or event.terminal_id != binding.terminal_id
            or event.conversation_id != binding.conversation_id
        ):
            raise AudioUploadError(404, "not_found")
        status = self.gateway.store.get_slice_status(event_id, user_id=binding.user_id)
        return web.json_response(status.model_dump(mode="json"))
