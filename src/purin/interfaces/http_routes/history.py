"""Device chat history and attachments, with scoped readers shared by the admin panel."""

from aiohttp import web

from .common import HTTP_INTERFACE, HTTPError

routes = web.RouteTableDef()


@routes.get("/v1/history")
async def chat_history(request: web.Request) -> web.Response:
    binding = request.app[HTTP_INTERFACE].authenticate(request)
    if request.query.get("view") != "dialogue":
        raise HTTPError(400, "invalid_query")
    return history_page(request, binding.user_id, binding.conversation_id)


def history_page(request: web.Request, user_id: str, conversation_id: str) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    query = request.query
    group_id = request.match_info.get("group_id")
    allowed = {"cursor", "limit"} if group_id else {"view", "cursor", "limit", "grouped"}
    if set(query) - allowed or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        limit = int(query.get("limit", "50"))
        if group_id:
            page, cursor = interface.gateway.store.history.browse_screenshot_group(
                user_id=user_id,
                conversation_id=conversation_id,
                group_id=group_id,
                cursor=query.get("cursor"),
                limit=limit,
            )
        else:
            grouped = query.get("grouped", "0")
            if grouped not in {"0", "1"}:
                raise ValueError("Invalid grouping option")
            page, cursor = interface.gateway.store.history.browse_history(
                user_id=user_id,
                conversation_id=conversation_id,
                view=query.get("view", ""),
                grouped=grouped == "1",
                cursor=query.get("cursor"),
                limit=limit,
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
            **({"context_write": item.context_write} if item.context_write is not None else {}),
            **(
                {"material_type": item.material_type, "processing": item.processing}
                if item.material_type
                else {}
            ),
            **({"group": item.group} if item.group is not None else {}),
            **({"layer": item.layer} if item.layer is not None else {}),
            **(
                {"device_id": item.device_id, "message_id": item.message_id}
                if item.message_id is not None
                else {}
            ),
        }
        for item in page
    ]
    return web.json_response(
        {"items": items, "next_cursor": cursor}, headers={"Cache-Control": "private, no-store"}
    )


@routes.get("/v1/history/files/{id}")
async def chat_file(request: web.Request) -> web.StreamResponse:
    binding = request.app[HTTP_INTERFACE].authenticate(request)
    return history_file(request, binding.user_id, binding.conversation_id)


def history_file(
    request: web.Request, user_id: str, conversation_id: str, *, include_captures: bool = False
) -> web.StreamResponse:
    interface = request.app[HTTP_INTERFACE]
    image_id = request.match_info["id"]
    if image_id.startswith("doc_"):
        try:
            data = interface.gateway.store.chat_files.original((user_id, conversation_id, image_id))
        except LookupError:
            raise HTTPError(404, "not_found") from None
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Content-Disposition": "attachment", "Cache-Control": "private, no-store"},
        )
    if ":chat:" in image_id:
        try:
            data = interface.gateway.store.chat_image(image_id, user_id, conversation_id)
        except LookupError:
            raise HTTPError(404, "not_found") from None
        return web.Response(
            body=data, content_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
        )
    if not include_captures:
        raise HTTPError(404, "not_found")
    return interface.history_files.download(
        interface.gateway.store, user_id, conversation_id, image_id
    )
