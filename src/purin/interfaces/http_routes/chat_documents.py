"""Authenticated upload, format detection and conversation-local conversion cache."""

import asyncio
import hashlib
import json
import sys
import unicodedata
from pathlib import PurePath
from uuid import uuid4

from aiohttp import web
from pydantic import Field, field_validator

from purin.config import ConfigError

from .common import HTTP_INTERFACE, HTTPError, HTTPInput

routes = web.RouteTableDef()
MAX_FILE_BYTES = 32 * 1024 * 1024


def filename(value: str) -> str:
    if (
        not value.strip()
        or len(value) > 255
        or value in (".", "..")
        or any(c in "/\\" or unicodedata.category(c) in {"Cc", "Cs"} for c in value)
    ):
        raise ValueError("Invalid filename")
    return value


class DocumentRef(HTTPInput):
    id: str = Field(pattern=r"^doc_[0-9a-f]{64}$")
    name: str

    _name = field_validator("name")(filename)


class WithDocuments(HTTPInput):
    documents: list[DocumentRef] = Field(default_factory=list, max_length=4)


async def convert_upload(data: bytes, extension: str, asr: dict[str, object]) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "purin.interfaces.document_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(220):
            header = json.dumps({"extension": extension, "asr": asr}).encode() + b"\n"
            output, _ = await process.communicate(header + data)
    except TimeoutError:
        raise HTTPError(504, "file_conversion_timeout") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        status, error = {
            2: (415, "unsupported_file"),
            4: (503, "asr_key_missing"),
            5: (502, "audio_conversion_failed"),
        }.get(process.returncode, (422, "file_conversion_failed"))
        raise HTTPError(status, error)
    return output.decode("utf-8")


@routes.post("/v1/chat/files")
async def upload(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.content_type != "application/octet-stream" or request.headers.get(
        "Content-Encoding"
    ):
        raise HTTPError(415, "binary_file_required")
    try:
        name = filename(request.query["name"])
    except (KeyError, ValueError):
        raise HTTPError(400, "invalid_filename") from None
    if interface.chat_media_slots.locked():
        raise HTTPError(429, "chat_media_busy")
    async with interface.chat_media_slots:
        try:
            async with asyncio.timeout(90):
                data = await request.clone(client_max_size=MAX_FILE_BYTES + 1).read()
        except TimeoutError:
            raise HTTPError(408, "upload_timeout") from None
        if len(data) > MAX_FILE_BYTES:
            raise HTTPError(413, "payload_too_large")
        extension = PurePath(name).suffix.lower()
        digest = hashlib.sha256(extension.encode() + b"\0")
        digest.update(data)
        file_id = "doc_" + digest.hexdigest()
        scope = binding.user_id, binding.conversation_id, file_id
        store = interface.gateway.store.chat_files
        owner = uuid4().hex
        try:
            async with asyncio.timeout(305):
                # A converter in another process can own the SQLite lease.
                while (state := store.claim(scope, owner)) == "busy":  # noqa: ASYNC110
                    await asyncio.sleep(0.25)
        except TimeoutError:
            raise HTTPError(503, "file_conversion_busy") from None
        if state == "new":
            try:
                try:
                    key = interface.asr_key()
                except ConfigError:
                    key = ""
                markdown = await convert_upload(
                    data,
                    extension,
                    {
                        "endpoint": interface.chat_asr.endpoint,
                        "api_key": key,
                        "timeout": interface.chat_asr.timeout,
                    },
                )
                store.finish(scope, owner, data, markdown)
            except BaseException:
                store.release(scope, owner)
                raise
        return web.json_response(
            {"id": file_id, "name": name}, headers={"Cache-Control": "no-store"}
        )
