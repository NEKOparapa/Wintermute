"""当前会话的日程查询。"""

from datetime import datetime

from aiohttp import web

from .common import HTTP_INTERFACE, HTTPError

routes = web.RouteTableDef()


@routes.get("/v1/schedule")
async def schedule(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    allowed = {"start", "end", "limit", "cursor"}
    if not {"start", "end"} <= set(query) <= allowed or any(
        len(query.getall(key)) != 1 for key in query
    ):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.list_schedules(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            start=datetime.fromisoformat(query["start"]),
            end=datetime.fromisoformat(query["end"]),
            limit=int(query.get("limit", "100")),
            cursor=query.get("cursor"),
        )
    except (ValueError, OverflowError):
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor},
        headers={"Cache-Control": "private, no-store"},
    )
