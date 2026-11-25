"""异步提问卡片的查询、回答与忽略路由。"""

from typing import Literal

from aiohttp import web
from pydantic import Field

from purin.events.storage.repositories.questions import (
    MAX_ANSWER_LEN,
    MAX_OPTIONS,
    QuestionConflict,
)

from .common import HTTP_INTERFACE, HTTPError, HTTPInput, read_input

routes = web.RouteTableDef()


class AnswerInput(HTTPInput):
    selected_options: list[str] = Field(default_factory=list, max_length=MAX_OPTIONS)
    answer_text: str = Field(default="", max_length=MAX_ANSWER_LEN)


@routes.get("/v1/questions")
async def list_questions(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    if set(query) - {"cursor", "limit", "status"} or any(
        len(query.getall(key)) != 1 for key in query
    ):
        raise HTTPError(400, "invalid_query")
    raw_status = query.get("status", "pending")
    if raw_status == "pending":
        status: Literal["pending", "all"] = "pending"
    elif raw_status == "all":
        status = "all"
    else:
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.questions.list(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            status=status,
            limit=int(query.get("limit", "20")),
            cursor=query.get("cursor"),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor},
        headers={"Cache-Control": "private, no-store"},
    )


@routes.post("/v1/questions/{id}/answer")
async def answer_question(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    data = await read_input(request, AnswerInput, "invalid_question_answer")
    try:
        item = interface.gateway.store.questions.answer(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            device_id=binding.device_id,
            id=request.match_info["id"],
            selected_options=data.selected_options,
            answer_text=data.answer_text,
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except QuestionConflict:
        raise HTTPError(409, "question_already_resolved") from None
    except ValueError:
        raise HTTPError(400, "invalid_question_answer") from None
    interface.gateway.wakeup()
    return web.json_response(
        item.model_dump(mode="json"),
        headers={"Cache-Control": "private, no-store"},
    )


@routes.post("/v1/questions/{id}/dismiss")
async def dismiss_question(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    try:
        item = interface.gateway.store.questions.dismiss(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            device_id=binding.device_id,
            id=request.match_info["id"],
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except QuestionConflict:
        raise HTTPError(409, "question_already_resolved") from None
    except ValueError:
        raise HTTPError(400, "invalid_question_answer") from None
    interface.gateway.wakeup()
    return web.json_response(
        item.model_dump(mode="json"),
        headers={"Cache-Control": "private, no-store"},
    )
