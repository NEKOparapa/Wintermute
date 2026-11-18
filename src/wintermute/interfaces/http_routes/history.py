"""会话历史、感知轨迹、压缩记录和历史附件。"""

from aiohttp import web

from wintermute.events.core.presentation import origin_text, timestamp_text

from .common import HTTP_INTERFACE, HTTPError

routes = web.RouteTableDef()


@routes.get("/v1/history/screenshot-groups/{group_id}")
@routes.get("/v1/history")
async def history(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    group_id = request.match_info.get("group_id")
    allowed = {"cursor", "limit"} if group_id else {"view", "cursor", "limit", "grouped"}
    if set(query) - allowed or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        limit = int(query.get("limit", "50"))
        if group_id:
            page, cursor = interface.gateway.store.browse_screenshot_group(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                group_id=group_id,
                cursor=query.get("cursor"),
                limit=limit,
            )
        else:
            grouped = query.get("grouped", "0")
            if grouped not in {"0", "1"}:
                raise ValueError("Invalid grouping option")
            page, cursor = interface.gateway.store.browse_history(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                view=query.get("view", ""),
                cursor=query.get("cursor"),
                limit=limit,
                grouped=grouped == "1",
            )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    items = [
        {
            "id": item.id,
            "role": item.role,
            "source": item.source,
            "occurred_at": item.occurred_at,
            "text": item.text,
            "files": [f for event in item.media for f in interface.history_files.describe(event)],
            **({"raw": item.raw.model_dump(mode="json")} if item.raw is not None else {}),
            **({"filter_result": item.filter_result} if item.filter_result else {}),
            **({"summary_round": item.summary_round} if item.summary_round is not None else {}),
            **({"usage": item.usage} if item.usage is not None else {}),
            **(
                {
                    "context_messages": item.context_messages,
                    "pending_context": item.pending_context,
                }
                if item.raw is not None
                else {}
            ),
            **({"members": item.members} if item.members else {}),
            **({"merged_summary": item.merged_summary} if item.merged_summary is not None else {}),
            **(
                {"material_type": item.material_type, "processing": item.processing}
                if item.material_type
                else {}
            ),
            **({"group": item.group} if item.group is not None else {}),
            **({"layer": item.layer} if item.layer is not None else {}),
            **(
                {"terminal_id": item.terminal_id, "message_id": item.message_id}
                if item.message_id is not None
                else {}
            ),
        }
        for item in page
    ]
    return web.json_response(
        {"items": items, "next_cursor": cursor}, headers={"Cache-Control": "private, no-store"}
    )


@routes.get("/v1/history/compression/{batch_id}")
@routes.get("/v1/history/compression/sources/{source_id}")
@routes.get("/v1/history/compression")
async def compression_history(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    store = interface.gateway.store
    query = request.query
    batch_id, source_id = (
        request.match_info.get("batch_id"),
        request.match_info.get("source_id"),
    )
    allowed = {"cursor", "limit"}
    if source_id is not None:
        allowed.add("activity")
    elif batch_id is None:
        allowed.add("layer")
    if set(query) - allowed or any(len(query.getall(key)) != 1 for key in query):
        raise HTTPError(400, "invalid_query")
    try:
        limit = int(query.get("limit", "20"))
        cursor = query.get("cursor")
        if source_id is not None:
            activity = query.get("activity", "0")
            if activity not in {"0", "1"}:
                raise ValueError("Invalid activity flag")
            sources = store.browse_compression_sources(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                source_id=source_id,
                activity=activity == "1",
                cursor=cursor,
                limit=limit,
            )
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
                            event.terminal_id,
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
        elif batch_id is not None:
            page = store.browse_compression_batch(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                batch_id=batch_id,
                limit=limit,
                cursor=cursor,
                ai_enabled=interface.timeline_ai_enabled,
            )
        else:
            page = store.browse_compression_batches(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                layer=query.get("layer", ""),
                limit=limit,
                cursor=cursor,
                ai_enabled=interface.timeline_ai_enabled,
            )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(page, headers={"Cache-Control": "private, no-store"})


@routes.get("/v1/history/perceptions/{event_id}/stages")
async def perception_stages(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query and list(request.query.items()) != [("record", "1")]:
        raise HTTPError(400, "invalid_query")
    try:
        result = interface.gateway.store.perception_stages(
            binding.user_id,
            binding.conversation_id,
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
            "source": event.terminal_id,
            "role": "event",
            "text": "",
            "raw": event.model_dump(mode="json"),
            "files": interface.history_files.describe(event),
            "context_messages": result["context_messages"],
        }
    return web.json_response(result, headers={"Cache-Control": "private, no-store"})


@routes.get("/v1/history/traces/{target}")
async def trace_members(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    if set(query) - {"cursor", "limit"} or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        result = interface.gateway.store.trace_members(
            binding.user_id,
            binding.conversation_id,
            request.match_info["target"],
            query.get("cursor"),
            int(query.get("limit", "40")),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    except LookupError:
        raise HTTPError(404, "not_found") from None
    return web.json_response(result, headers={"Cache-Control": "private, no-store"})


@routes.get("/v1/history/files/{id}")
async def history_file(request: web.Request) -> web.FileResponse:
    interface = request.app[HTTP_INTERFACE]
    return interface.history_files.download(
        interface.gateway.store, interface.authenticate(request), request.match_info["id"]
    )
