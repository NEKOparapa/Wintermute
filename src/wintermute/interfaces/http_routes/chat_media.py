"""Bounded media submitted explicitly from a chat draft."""

import asyncio
import base64
import io
import wave

from aiohttp import web
from pydantic import Field

from wintermute.config import ConfigError
from wintermute.events.slices.asr import AudioTranscriptionError
from wintermute.interfaces.image import validate_jpeg

from .chat_documents import WithDocuments, filename
from .common import HTTP_INTERFACE, ClientId, HTTPError, read_input

routes = web.RouteTableDef()
MAX_IMAGE = 512 * 1024
MAX_IMAGES = 4
MAX_WAV = 44 + 120 * 16000 * 2


class ImageMessageInput(WithDocuments):
    message_id: ClientId
    text: str
    images: list[str] = Field(min_length=1, max_length=MAX_IMAGES)
    image_names: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)


def decode_images(encoded: list[str]) -> tuple[bytes, ...]:
    images = []
    for url in encoded:
        prefix = "data:image/jpeg;base64,"
        if not url.startswith(prefix) or len(url) > len(prefix) + 4 * ((MAX_IMAGE + 2) // 3):
            raise ValueError("Invalid chat image")
        data = base64.b64decode(url[len(prefix) :], validate=True)
        if len(data) > MAX_IMAGE:
            raise ValueError("Chat image too large")
        validate_jpeg(data)
        images.append(data)
    return tuple(images)


async def read_image_message(request: web.Request) -> tuple[ImageMessageInput, tuple[bytes, ...]]:
    interface = request.app[HTTP_INTERFACE]
    if request.headers.get("Content-Encoding"):
        raise HTTPError(415, "unsupported_encoding")
    if interface.chat_media_slots.locked():
        raise HTTPError(429, "chat_media_busy")
    async with interface.chat_media_slots:
        limit = interface.settings.max_body_bytes + MAX_IMAGES * (4 * ((MAX_IMAGE + 2) // 3) + 128)
        try:
            async with asyncio.timeout(60):
                message = await read_input(
                    request.clone(client_max_size=limit), ImageMessageInput, "invalid_image_message"
                )
            if len(message.text.encode()) > interface.settings.max_body_bytes:
                raise HTTPError(413, "payload_too_large")
            if len(message.images) + len(message.documents) > 4 or (
                message.image_names and len(message.image_names) != len(message.images)
            ):
                raise HTTPError(400, "invalid_attachments")
            for name in message.image_names:
                filename(name)
            images = await asyncio.to_thread(decode_images, message.images)
        except TimeoutError:
            raise HTTPError(408, "upload_timeout") from None
        except ValueError:
            raise HTTPError(400, "invalid_chat_image") from None
    return message, images


@routes.post("/v1/transcriptions")
async def transcribe(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    interface.authenticate(request)
    if request.content_type != "audio/wav" or request.headers.get("Content-Encoding"):
        raise HTTPError(415, "wav_required")
    if interface.chat_media_slots.locked():
        raise HTTPError(429, "chat_media_busy")
    async with interface.chat_media_slots:
        try:
            async with asyncio.timeout(60):
                audio = await request.clone(client_max_size=MAX_WAV + 1).read()
        except TimeoutError:
            raise HTTPError(408, "upload_timeout") from None
        try:
            with wave.open(io.BytesIO(audio)) as wav:
                frames = wav.getnframes()
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                    raise ValueError
                if not 0 < frames <= 120 * 16000 or len(wav.readframes(frames)) != frames * 2:
                    raise ValueError
        except (ValueError, wave.Error, EOFError):
            raise HTTPError(400, "invalid_wav") from None
        try:
            transcript = await interface.chat_asr.transcribe_parts([("audio/wav", audio)])
        except ConfigError:
            raise HTTPError(503, "asr_key_missing") from None
        except AudioTranscriptionError:
            raise HTTPError(502, "transcription_failed") from None
        return web.json_response({"text": transcript}, headers={"Cache-Control": "no-store"})
