"""终端工具领取、结果和调用历史。"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from typing import Literal

from aiohttp import web
from pydantic import Field, field_validator

from wintermute.events.integrations.terminal_tool import ToolName
from wintermute.events.storage.repositories.remote_tools import RemoteCallConflict
from wintermute.logging import get_logger

from .common import HTTP_INTERFACE, HTTPError, HTTPInput, read_input

routes = web.RouteTableDef()
logger = get_logger(__name__)


class ClaimInput(HTTPInput):
    tools: list[ToolName] = Field(max_length=32)

    @field_validator("tools")
    @classmethod
    def distinct(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names):
            raise ValueError("Repeated terminal tool")
        return names


class ResultInput(HTTPInput):
    status: Literal["succeeded", "failed", "timed_out"]
    claim_token: str = Field(min_length=1, max_length=256, repr=False)
    stdout: str = Field(max_length=8192)
    stderr: str = Field(max_length=8192)
    exit_code: int | None
    truncated: bool


async def lifetime(app: web.Application) -> AsyncIterator[None]:
    tools = app[HTTP_INTERFACE].gateway.store.remote_tools

    async def expire_pending() -> None:
        while True:
            try:
                tools.expire()
            except sqlite3.OperationalError as exc:
                logger.warning("终端工具过期检查失败", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(1)

    task = asyncio.create_task(expire_pending())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@routes.post("/v1/terminal-tools/claim")
async def tool_claim(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate_tool(request)
    data = await read_input(request, ClaimInput, "invalid_terminal_tool_claim")
    interface.terminal_capabilities.update(binding.terminal_id, data.tools)
    call = (
        interface.gateway.store.remote_tools.claim(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            terminal_id=binding.terminal_id,
            tools=data.tools,
        )
        if data.tools
        else None
    )
    return web.json_response({"call": call.model_dump(mode="json") if call else None})


@routes.post("/v1/terminal-tools/{id}/result")
async def tool_result(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate_tool(request)
    data = await read_input(request, ResultInput, "invalid_terminal_tool_result")
    try:
        call = interface.gateway.store.remote_tools.complete(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            terminal_id=binding.terminal_id,
            id=request.match_info["id"],
            claim_token=data.claim_token,
            status=data.status,
            stdout=data.stdout,
            stderr=data.stderr,
            exit_code=data.exit_code,
            truncated=data.truncated,
        )
    except LookupError:
        raise HTTPError(404, "not_found") from None
    except RemoteCallConflict:
        raise HTTPError(409, "terminal_tool_conflict") from None
    except ValueError:
        raise HTTPError(400, "invalid_terminal_tool_result") from None
    return web.json_response({"id": call.id, "status": call.status})


@routes.get("/v1/terminal-tools/history")
async def tool_history(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    if set(query) - {"cursor", "limit"} or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.remote_tools.list(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            limit=int(query.get("limit", "30")),
            cursor=query.get("cursor"),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor}
    )
