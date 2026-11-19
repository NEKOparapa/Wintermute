"""Authenticated, user-scoped Markdown memory management."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web
from pydantic import Field

from wintermute.memory import MemoryConflictError, MemoryVault

from .common import HTTP_INTERFACE, HTTPError, HTTPInput, read_input

routes = web.RouteTableDef()


class _SaveInput(HTTPInput):
    content: str
    reason: str = Field(default="用户手动编辑记忆", min_length=2, max_length=300)
    expected_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{16}$")


class _ScheduleInput(HTTPInput):
    interval_days: int = Field(ge=1, le=3650)
    local_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


@routes.get("/v1/memory/schedule")
@routes.post("/v1/memory/schedule")
async def memory_schedule(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    user = interface.authenticate(request).user_id
    if user not in interface.memory_vaults:
        raise HTTPError(503, "memory_unconfigured")
    if request.query:
        raise HTTPError(400, "invalid_query")
    memory = interface.gateway.store.memory
    if request.method == "POST":
        data = await read_input(request, _ScheduleInput, "invalid_memory_schedule")
        plan = memory.set_schedule(user, data.interval_days, data.local_time)
        interface.gateway.wakeup()
    else:
        plan = memory.schedule(user)
    return web.json_response(
        {
            "interval_days": plan.interval_days,
            "local_time": plan.local_time,
            "timezone": plan.zone.key,
            "next_run_at": plan.next_run_at.isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )


def _vault(request: web.Request) -> MemoryVault:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    vault = interface.memory_vaults.get(binding.user_id)
    if vault is None:
        raise HTTPError(503, "memory_unconfigured")
    return vault


def _query(request: web.Request, *, optional: set[str] | None = None) -> str:
    allowed = {"path", *(optional or set())}
    if any(key not in allowed or len(request.query.getall(key)) != 1 for key in request.query):
        raise HTTPError(400, "invalid_query")
    path = request.query.get("path", "")
    if not path:
        raise HTTPError(400, "path_required")
    return path


async def _handle(operation: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await operation()
    except FileNotFoundError:
        raise HTTPError(404, "memory_file_not_found") from None
    except FileExistsError:
        raise HTTPError(409, "memory_conflict") from None
    except PermissionError:
        raise HTTPError(403, "memory_forbidden") from None
    except MemoryConflictError:
        raise HTTPError(409, "memory_conflict") from None
    except ValueError:
        raise HTTPError(400, "invalid_memory_request") from None
    except OSError:
        raise HTTPError(503, "memory_unavailable") from None


def _document(vault: MemoryVault, path: str) -> dict[str, object]:
    content = vault.read_text(path)
    return {
        "path": path,
        "content": content,
        "content_sha256": sha256(content.encode("utf-8")).hexdigest()[:16],
    }


@routes.get("/v1/memory/tree")
async def tree(request: web.Request) -> web.Response:
    vault = _vault(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    files = await _handle(lambda: asyncio.to_thread(vault.list_files, max_depth=5))
    return web.json_response(
        {
            "directories": ["agent-character", "about-user", "note", "log"],
            "files": files,
        }
    )


@routes.get("/v1/memory/file")
async def read_file(request: web.Request) -> web.Response:
    vault = _vault(request)
    path = _query(request)
    document = await _handle(lambda: asyncio.to_thread(_document, vault, path))
    return web.json_response(document)


@routes.put("/v1/memory/file")
async def save_file(request: web.Request) -> web.Response:
    vault = _vault(request)
    path = _query(request)
    # JSON escapes may expand a 128 KiB Markdown document; only this route gets the larger limit.
    data = await read_input(
        request.clone(client_max_size=1024 * 1024), _SaveInput, "invalid_memory_request"
    )
    result = await _handle(
        lambda: vault.write(
            "save",
            path,
            actor="human_api",
            reason=data.reason,
            content=data.content,
            expected_hash=data.expected_hash,
        )
    )
    return web.json_response(result)


@routes.delete("/v1/memory/file")
async def delete_file(request: web.Request) -> web.Response:
    vault = _vault(request)
    path = _query(request, optional={"expected_hash", "reason"})
    result = await _handle(
        lambda: vault.write(
            "delete",
            path,
            actor="human_api",
            reason=request.query.get("reason", "用户手动删除记忆"),
            expected_hash=request.query.get("expected_hash"),
        )
    )
    return web.json_response(result)


@routes.get("/v1/memory/logs")
async def logs(request: web.Request) -> web.Response:
    vault = _vault(request)
    if any(key != "date" or len(request.query.getall(key)) != 1 for key in request.query):
        raise HTTPError(400, "invalid_query")
    store = request.app[HTTP_INTERFACE].gateway.store
    today = store.now_utc().astimezone(ZoneInfo(store.timeline_policy.timezone)).date()
    value = request.query.get("date", today.isoformat())
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError("Non-canonical date")
    except ValueError:
        raise HTTPError(400, "invalid_date") from None
    document = await _handle(lambda: asyncio.to_thread(_document, vault, f"log/{day}.md"))
    return web.json_response(document)
