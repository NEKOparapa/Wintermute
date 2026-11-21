"""Authenticated batch ingestion; historical records never enter the live timeline."""

import json

from aiohttp import web

from wintermute.events.storage.repositories.records import (
    MAX_BATCH_BYTES,
    RecordBatchInput,
    RecordConflict,
)

from .common import HTTP_INTERFACE, HTTPError, _json_pairs, _reject_constant

routes = web.RouteTableDef()


@routes.post("/v1/records/batches")
async def submit_batch(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    if binding.user_id not in interface.memory_vaults:
        raise HTTPError(503, "memory_unconfigured")
    if "record" not in interface.gateway.store.get_device(binding.device_id).allowed_layers:
        raise HTTPError(403, "record_forbidden")
    if request.content_type != "application/json":
        raise HTTPError(415, "json_required")
    if request.content_length is not None and request.content_length > MAX_BATCH_BYTES:
        raise HTTPError(413, "payload_too_large")
    body = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > MAX_BATCH_BYTES:
            raise HTTPError(413, "payload_too_large")
    try:
        content = json.loads(body, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
        batch = RecordBatchInput.model_validate(content)
    except (ValueError, RecursionError):
        raise HTTPError(400, "invalid_record_batch") from None
    try:
        result = await interface.gateway.submit_records(binding.device_id, batch)
    except RecordConflict:
        raise HTTPError(409, "batch_id_conflict") from None
    except ValueError:
        raise HTTPError(400, "record_batch_rejected") from None
    except OSError:
        raise HTTPError(503, "record_storage_unavailable") from None
    return web.json_response(
        result.model_dump(mode="json"), status=202, headers={"Cache-Control": "private, no-store"}
    )


@routes.get("/v1/records/batches/{batch_id}")
async def get_batch(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    try:
        result = interface.gateway.store.records.get(
            binding.user_id, request.match_info["batch_id"]
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    return web.json_response(
        result.model_dump(mode="json"), headers={"Cache-Control": "private, no-store"}
    )
