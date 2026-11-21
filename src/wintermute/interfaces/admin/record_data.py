"""Read-only data for the authenticated administrator's history views."""

import json

from aiohttp import web

from wintermute.events.core import EventError
from wintermute.events.core.presentation import origin_text, timestamp_text
from wintermute.events.storage.repositories.records import MAX_BATCH_BYTES
from wintermute.interfaces.http_routes.common import HTTP_INTERFACE, HTTPError
from wintermute.interfaces.http_routes.history import history_file, history_page


def _scope(request: web.Request) -> tuple[str, str]:
    conversation = request.match_info["admin_conversation"]
    try:
        user = request.app[HTTP_INTERFACE].gateway.store.get_conversation_user(conversation)
    except EventError:
        raise HTTPError(404, "conversation_not_found") from None
    return user, conversation


async def history(request: web.Request) -> web.Response:
    return history_page(request, *_scope(request))


async def attachment(request: web.Request) -> web.StreamResponse:
    return history_file(request, *_scope(request), include_captures=True)


async def compression_sources(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user_id, conversation_id = _scope(request)
    query = request.query
    if set(query) - {"cursor", "limit", "activity"} or any(
        len(query.getall(k)) != 1 for k in query
    ):
        raise HTTPError(400, "invalid_query")
    try:
        activity = query.get("activity", "0")
        if activity not in {"0", "1"}:
            raise ValueError("Invalid activity flag")
        sources = interface.gateway.store.history.browse_compression_sources(
            user_id=user_id,
            conversation_id=conversation_id,
            source_id=request.match_info["source_id"],
            activity=activity == "1",
            cursor=query.get("cursor"),
            limit=int(query.get("limit", "20")),
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    rows = []
    for record in sources.items:
        event = record.event
        rows.append(
            {
                "id": event.event_id,
                "kind": event.type,
                "occurred_at": timestamp_text(event.occurred_at),
                "source": origin_text(
                    "感知" if event.layer == "perception" else "切片",
                    event.device_id,
                    event.source_id,
                ),
                "payload": event.payload,
                "transcript": record.transcript,
                "files": interface.history_files.describe(event),
            }
        )
    page = {
        "items": rows,
        "next_cursor": sources.next_cursor,
        "extraction": sources.extraction,
    }
    return web.json_response(page)


async def perception_stages(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user_id, conversation_id = _scope(request)
    if request.query and list(request.query.items()) != [("record", "1")]:
        raise HTTPError(400, "invalid_query")
    try:
        result = interface.gateway.store.history.perception_stages(
            user_id,
            conversation_id,
            request.match_info["event_id"],
            ai_enabled=interface.timeline_ai_enabled,
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    if request.query:
        # The scoped stages lookup above authorizes this specific journal record.
        event = interface.gateway.store.get_event(request.match_info["event_id"])
        result["record"] = {
            "id": event.event_id,
            "occurred_at": timestamp_text(event.occurred_at),
            "source": event.device_id,
            "role": "event",
            "text": "",
            "raw": event.model_dump(mode="json"),
            "files": interface.history_files.describe(event),
            "context_messages": result["context_messages"],
        }
    return web.json_response(result, headers={"Cache-Control": "private, no-store"})


async def trace_members(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user_id, conversation_id = _scope(request)
    query = request.query
    if set(query) - {"cursor", "limit"} or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        result = interface.gateway.store.history.trace_members(
            user_id,
            conversation_id,
            request.match_info["target"],
            query.get("cursor"),
            int(query.get("limit", "40")),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    except LookupError:
        raise HTTPError(404, "not_found") from None
    return web.json_response(result, headers={"Cache-Control": "private, no-store"})


async def list_items(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user_id, _ = _scope(request)
    if set(request.query) - {"limit", "cursor", "record_type"} or any(
        len(request.query.getall(key)) != 1 for key in request.query
    ):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.records.list_items(
            user_id,
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


async def get_item_content(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user_id, _ = _scope(request)
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
        item = records.get_item(user_id, request.match_info["item_id"])
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


async def connector_history(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user_id, conversation_id = _scope(request)
    query = request.query
    if set(query) - {"cursor", "limit"} or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.connectors.list_history(
            user_id=user_id,
            conversation_id=conversation_id,
            limit=int(query.get("limit", "30")),
            cursor=query.get("cursor"),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor}
    )
