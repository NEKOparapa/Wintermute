"""Authenticated batch ingestion; historical records never enter the live timeline."""

import json

from aiohttp import web

from wintermute.events.core.types import EventError
from wintermute.events.storage.records import MAX_BATCH_BYTES, RecordBatchInput, RecordConflict

from .common import HTTP_INTERFACE, HTTPError, _json_pairs, _reject_constant

routes = web.RouteTableDef()


@routes.get("/v1/records/items")
async def list_items(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if set(request.query) - {"limit", "cursor", "record_type"} or any(
        len(request.query.getall(key)) != 1 for key in request.query
    ):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.records.list_items(
            binding.user_id,
            limit=int(request.query.get("limit", "40")),
            cursor=request.query.get("cursor"),
            record_type=request.query.get("record_type"),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {
            "items": [
                item.model_dump(mode="json", exclude={"user_id", "content_sha256"})
                for item in items
            ],
            "next_cursor": cursor,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@routes.get("/v1/records/items/{item_id}")
async def get_item_content(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if set(request.query) - {"offset", "limit"} or any(
        len(request.query.getall(key)) != 1 for key in request.query
    ):
        raise HTTPError(400, "invalid_query")
    try:
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "16000"))
        if not 0 <= offset <= MAX_BATCH_BYTES * 6 or not 1 <= limit <= 64000:
            raise ValueError("Invalid content range")
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    records = interface.gateway.store.records
    try:
        item = records.get_item(binding.user_id, request.match_info["item_id"])
        payload = records.payload(item)
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except (OSError, ValueError, EventError):
        raise HTTPError(503, "record_storage_unavailable") from None
    text = (
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if offset > len(text):
        raise HTTPError(400, "invalid_query")
    end = min(offset + limit, len(text))
    return web.json_response(
        {
            "item_id": item.item_id,
            "text": text[offset:end],
            "format": "text" if isinstance(payload, str) else "json",
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
        },
        headers={"Cache-Control": "private, no-store"},
    )


@routes.post("/v1/records/batches")
async def submit_batch(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    if binding.user_id not in interface.memory_vaults:
        raise HTTPError(503, "memory_unconfigured")
    if "record" not in interface.gateway.store.get_terminal(binding.terminal_id).allowed_layers:
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
        result = await interface.gateway.submit_records(binding.terminal_id, batch)
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


@routes.get("/v1/records/batches")
async def list_batches(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if set(request.query) - {"limit", "cursor"} or any(
        len(request.query.getall(key)) != 1 for key in request.query
    ):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.records.list(
            binding.user_id,
            limit=int(request.query.get("limit", "20")),
            cursor=request.query.get("cursor"),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor},
        headers={"Cache-Control": "private, no-store"},
    )
