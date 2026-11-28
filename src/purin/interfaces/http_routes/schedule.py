"""当前会话的日程查询。"""

from datetime import datetime

from aiohttp import web

from .common import HTTP_INTERFACE, HTTPError
from .notifications import notice_data

routes = web.RouteTableDef()


@routes.get("/v1/schedule")
async def schedule(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    allowed = {"start", "end", "limit", "cursor", "include_notifications", "view"}
    if not {"start", "end"} <= set(query) <= allowed or any(
        len(query.getall(key)) != 1 for key in query
    ):
        raise HTTPError(400, "invalid_query")
    include = query.get("include_notifications", "0")
    view = query.get("view", "full")
    if include not in {"0", "1"} or view not in {"full", "summary"}:
        raise HTTPError(400, "invalid_query")
    store = interface.gateway.store
    try:
        items, cursor = store.schedules.calendar_page(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            start=datetime.fromisoformat(query["start"]),
            end=datetime.fromisoformat(query["end"]),
            limit=int(query.get("limit", "100")),
            cursor=query.get("cursor"),
            inbox_device=binding.device_id if include == "1" else None,
            notification_details=view == "full",
        )
    except (ValueError, OverflowError):
        raise HTTPError(400, "invalid_query") from None
    names = interface.devices.display_names(binding.user_id) if any(n for _, n in items) else {}
    rows = []
    for item, notice in items:
        if view == "summary":
            row = item.model_dump(mode="json", include={"title", "action", "trigger_at"})
            row["content"] = item.content[:80] + ("…" if len(item.content) > 80 else "")
            rows.append(row)
            continue
        row = item.model_dump(mode="json")
        if notice is not None:
            row["notification"] = notice_data(notice, names)
        rows.append(row)
    return web.json_response(
        {"items": rows, "next_cursor": cursor},
        headers={"Cache-Control": "private, no-store"},
    )
