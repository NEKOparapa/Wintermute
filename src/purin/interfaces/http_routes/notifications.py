"""通知收件箱、阅读状态和附件下载。"""

from urllib.parse import quote

from aiohttp import web

from purin.events.storage.notifications import NotificationItem

from .common import HTTP_INTERFACE, HTTPError

routes = web.RouteTableDef()


def notice_data(item: NotificationItem, names: dict[str, str]) -> dict[str, object]:
    return {**item, "target_names": [names.get(id, id) for id in item["targets"]]}


@routes.get("/v1/notifications")
async def notifications(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    if set(query) - {"cursor", "limit", "pending", "unread"} or any(
        len(query.getall(key)) != 1 for key in query
    ):
        raise HTTPError(400, "invalid_query")
    pending, unread = query.get("pending", "0"), query.get("unread", "0")
    if pending not in {"0", "1"} or unread not in {"0", "1"} or pending == unread == "1":
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.notifications.list_user_notifications(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            device_id=binding.device_id,
            cursor=query.get("cursor"),
            limit=int(query.get("limit", "50")),
            pending_only=pending == "1",
            unread_only=unread == "1",
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    names = interface.devices.display_names(binding.user_id) if items else {}
    return web.json_response(
        {"items": [notice_data(item, names) for item in items], "next_cursor": cursor},
        headers={"Cache-Control": "private, no-store"},
    )


@routes.post("/v1/notifications/{id}/{state:received|read}")
async def notification_ack(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    try:
        item = interface.gateway.store.notifications.ack_user_notification(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            device_id=binding.device_id,
            notification_id=request.match_info["id"],
            read=request.match_info["state"] == "read",
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    return web.json_response(
        notice_data(item, interface.devices.display_names(binding.user_id)),
        headers={"Cache-Control": "private, no-store"},
    )


@routes.get("/v1/notifications/{id}/files/{file_id}")
async def notification_file(request: web.Request) -> web.StreamResponse:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    store = interface.gateway.store
    try:
        file, rowid = store.notifications.notification_file(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            device_id=binding.device_id,
            notification_id=request.match_info["id"],
            file_id=request.match_info["file_id"],
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    size = file["size_bytes"]
    response = web.StreamResponse(
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file["name"], safe=""),
            "Content-Type": file["content_type"],
            "Content-Length": str(size),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
    )
    await response.prepare(request)
    if request.method != "HEAD":
        for offset in range(0, size, 64 * 1024):
            await response.write(
                store.notifications.read_notification_chunk(
                    rowid, offset, min(size - offset, 64 * 1024)
                )
            )
    await response.write_eof()
    return response
