"""带终端认证的 HTTP 交流、设备采集入口和持久化输出收件箱。"""

import asyncio
import hmac
import ipaddress
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator

from wintermute.agent import AssistantMessage, ToolResultMessage, UserMessage
from wintermute.agent.context.types import history_digest
from wintermute.config.models import (
    AudioSliceSettings,
    HTTPSettings,
    HTTPTerminalSettings,
    ImageSliceSettings,
)
from wintermute.events import (
    DuplicateEventError,
    EventError,
    EventGateway,
    Layer,
    OutputRecord,
    Receipt,
    Terminal,
)
from wintermute.events.core.desktop import WINDOW_SPAN_STATE_KEY
from wintermute.events.core.presentation import origin_text, timestamp_text
from wintermute.events.core.timeline_compression import estimate
from wintermute.events.integrations.terminal_tool import TerminalCapabilities, ToolName
from wintermute.events.runtime.profile import (
    ProfileDocument,
    read_profile_document,
    read_profile_prompt,
)
from wintermute.events.slices.image import ImageSliceProcessor
from wintermute.events.storage.remote_tools import RemoteCallConflict
from wintermute.interfaces.audio import AudioUploadError, AudioUploads
from wintermute.interfaces.desktop_window import DIGEST_PREFIX, DesktopWindowUploads
from wintermute.interfaces.history_files import HistoryFiles
from wintermute.interfaces.image import ImageUploads
from wintermute.logging import get_logger, log_context

if TYPE_CHECKING:
    from wintermute.config.api_keys import APIKeyRegistry

logger = get_logger(__name__)


type _ClientId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
type _Text = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _HTTPInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class _MessageInput(_HTTPInput):
    message_id: _ClientId
    text: _Text


class ClaimInput(_HTTPInput):
    tools: list[ToolName] = Field(max_length=32)

    @field_validator("tools")
    @classmethod
    def distinct(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names):
            raise ValueError("Repeated terminal tool")
        return names


class ResultInput(_HTTPInput):
    status: Literal["succeeded", "failed", "timed_out"]
    claim_token: str = Field(min_length=1, max_length=256, repr=False)
    stdout: str = Field(max_length=8192)
    stderr: str = Field(max_length=8192)
    exit_code: int | None
    truncated: bool


class _APIKeyInput(_HTTPInput):
    id: _ClientId
    api_key: str = Field(repr=False)


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _key_transport_allowed(request: web.Request) -> bool:
    if request.secure:
        return True
    # 仅信任同机代理覆盖的协议头；外部直连不能用转发头伪装成 TLS。
    if not _is_loopback(request.remote):
        return False
    protocols = request.headers.getall("X-Forwarded-Proto", [])
    if protocols:
        return protocols == ["https"]
    if any(name in request.headers for name in ("Forwarded", "X-Forwarded-For", "Via")):
        return False
    return _is_loopback(request.url.host)


class _CaptureInput(_HTTPInput):
    event_id: _ClientId
    occurred_at: AwareDatetime | None = None

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_iso_time(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value


class _PerceptionInput(_CaptureInput):
    state_key: _ClientId
    value: JsonValue


class _SliceInput(_CaptureInput):
    text: _Text


class _HTTPError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("JSON must contain finite numbers")


async def _read_input[T: _HTTPInput](request: web.Request, schema: type[T], error_code: str) -> T:
    if request.content_type != "application/json":
        raise _HTTPError(415, "json_required")
    try:
        body = json.loads(
            await request.text(), object_pairs_hook=_json_pairs, parse_constant=_reject_constant
        )
        return schema.model_validate(body)
    except (ValueError, RecursionError):
        raise _HTTPError(400, error_code) from None


def _output_data(output: OutputRecord) -> dict[str, Any]:
    # 投递异常可能包含外部请求细节；对外仅提供结果和投递状态。
    return output.model_dump(mode="json", exclude={"error", "user_id"})


class HTTPInterface:
    """将认证终端映射到固定用户和会话，模型运行由现有调度器负责。

    交流消息返回任务 ID，采集事件只返回入库回执。客户端在处理输出后
    显式调用 ack，断线、重启和通知结果共用同一个持久化收件箱。
    """

    def __init__(
        self,
        settings: HTTPSettings,
        gateway: EventGateway,
        *,
        api_keys: "APIKeyRegistry | None" = None,
        environ: Mapping[str, str] | None = None,
        audio_settings: AudioSliceSettings | None = None,
        image_settings: ImageSliceSettings | None = None,
        desktop_vision: ImageSliceProcessor | None = None,
        timeline_ai_enabled: bool = False,
        profile_files: Mapping[str, Path] | None = None,
        system_prompt: str = "",
        context_input_limit: int | None = None,
        base_dir: str | Path = ".",
    ) -> None:
        self.settings = settings.model_copy(deep=True)
        self.gateway = gateway
        self._api_keys = api_keys
        self.terminal_capabilities = TerminalCapabilities()
        self.images = ImageUploads(image_settings or ImageSliceSettings(), gateway, base_dir)
        self.desktop_windows = DesktopWindowUploads(
            gateway,
            self.images.directory,
            desktop_vision.model if desktop_vision else None,
            timeout=desktop_vision.timeout if desktop_vision else 60,
        )
        self.audio = AudioUploads(
            audio_settings or AudioSliceSettings(), gateway, base_dir=base_dir
        )
        self.history_files = HistoryFiles(self.images.directory, self.audio.directory)
        self.timeline_ai_enabled = timeline_ai_enabled
        self._profile_files = dict(profile_files or {})
        self._system_prompt = system_prompt
        self._context_input_limit = context_input_limit
        self._bindings = {item.terminal_id: item for item in self.settings.terminals}
        owners: dict[str, str] = {}
        for binding in self._bindings.values():
            owner = owners.setdefault(binding.conversation_id, binding.user_id)
            if owner != binding.user_id:
                raise ValueError("HTTP bindings assign a conversation to different users")
        self._tokens = {
            name: item.resolve_token(environ).encode("utf-8")
            for name, item in self._bindings.items()
        }
        for name, token in self._tokens.items():
            if self._bindings[name].manage_api_keys and any(
                other != name and token == other_token
                for other, other_token in self._tokens.items()
            ):
                raise ValueError("An API key management terminal requires a unique bearer token")
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._register_bindings()

    def _register_bindings(self) -> None:
        # 先检查全部已有归属，避免后面的错误留下部分注册配置。
        registrations: list[Terminal] = []
        for binding in self._bindings.values():
            http_layers: tuple[Layer, ...] = ("conversation", "perception")
            if not binding.is_desktop:
                http_layers += ("slice",)
            try:
                terminal = self.gateway.store.get_terminal(binding.terminal_id)
            except EventError:
                registrations.append(
                    Terminal(
                        terminal_id=binding.terminal_id,
                        user_id=binding.user_id,
                        allowed_layers=http_layers,
                    )
                )
            else:
                if (
                    terminal.user_id != binding.user_id
                    or "conversation" not in terminal.allowed_layers
                ):
                    raise ValueError("HTTP terminal conflicts with an existing registration")
                allowed = tuple(dict.fromkeys((*terminal.allowed_layers, *http_layers)))
                if binding.is_desktop:
                    allowed = tuple(layer for layer in allowed if layer != "slice")
                if allowed != terminal.allowed_layers:
                    registrations.append(terminal.model_copy(update={"allowed_layers": allowed}))
            try:
                owner = self.gateway.store.get_conversation_user(binding.conversation_id)
            except EventError:
                continue
            if owner != binding.user_id:
                raise ValueError("HTTP conversation conflicts with an existing registration")
        for terminal in registrations:
            self.gateway.register_terminal(terminal)
        for binding in self._bindings.values():
            self.gateway.register_conversation(binding.conversation_id, binding.user_id)

    def _authenticate(self, request: web.Request) -> HTTPTerminalSettings:
        if (
            len(request.headers.getall("X-Terminal-ID", [])) != 1
            or len(request.headers.getall("Authorization", [])) != 1
        ):
            raise _HTTPError(401, "unauthorized")
        terminal_id = request.headers.get("X-Terminal-ID", "")
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        expected = self._tokens.get(terminal_id)
        if (
            expected is None
            or not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(token.encode("utf-8"), expected)
        ):
            raise _HTTPError(401, "unauthorized")
        return self._bindings[terminal_id]

    def create_app(self) -> web.Application:
        """创建可交给 aiohttp runner 或测试客户端使用的应用。"""
        if not self.settings.enabled:
            raise ValueError("HTTP interface is disabled")
        if self._closed:
            raise RuntimeError("HTTP interface is closed")
        if self._app is not None:
            return self._app

        @web.middleware
        async def errors(
            request: web.Request,
            handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
        ) -> web.StreamResponse:
            try:
                return await handler(request)
            except (_HTTPError, AudioUploadError) as exc:
                headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
                return web.json_response({"error": exc.code}, status=exc.status, headers=headers)
            except web.HTTPException as exc:
                return web.json_response(
                    {"error": "payload_too_large" if exc.status == 413 else "http_error"},
                    status=exc.status,
                )
            except Exception as exc:
                # 不记录异常正文，第三方异常可能包含认证头、请求或模型内容。
                logger.error("HTTP 请求处理失败", extra={"error_type": type(exc).__name__})
                return web.json_response({"error": "internal_error"}, status=500)

        @web.middleware
        async def private_settings(
            request: web.Request,
            handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
        ) -> web.StreamResponse:
            response = await handler(request)
            if request.path == "/v1/settings/api-keys" or request.path.startswith(
                "/v1/terminal-tools/"
            ):
                response.headers["Cache-Control"] = "private, no-store"
            return response

        app = web.Application(
            client_max_size=self.settings.max_body_bytes,
            middlewares=[private_settings, errors],
        )
        app.on_startup.append(self.desktop_windows.recover)
        app.cleanup_ctx.append(self.desktop_windows.lifetime)
        app.cleanup_ctx.append(self._terminal_tools_lifetime)
        app.add_routes(
            [
                web.get("/health", self._health),
                web.post("/v1/messages", self._submit),
                web.post("/v1/terminal-tools/claim", self._tool_claim),
                web.post("/v1/terminal-tools/{id}/result", self._tool_result),
                web.get("/v1/terminal-tools/history", self._tool_history),
                web.post("/v1/perceptions", self._perception),
                web.post("/v1/perceptions/desktop-window", self._desktop_window),
                web.post("/v1/perceptions/desktop-window/batches", self._desktop_window),
                web.post("/v1/slices", self._text_slice),
                web.post("/v1/audio/slices", self._audio_submit),
                web.post("/v1/image/slices", self._image_submit),
                web.get("/v1/image/slices/{event_id}", self._media_status),
                web.get("/v1/audio/slices/{event_id}", self._media_status),
                web.get("/v1/jobs/{job_id}", self._job),
                web.get("/v1/schedule", self._schedule),
                web.get("/v1/profile", self._profile),
                web.get("/v1/settings/api-keys", self._manage_api_keys),
                web.post("/v1/settings/api-keys", self._manage_api_keys),
                web.get("/v1/notifications", self._notifications),
                web.post("/v1/notifications/{id}/{state:received|read}", self._notification_ack),
                web.get("/v1/notifications/{id}/files/{file_id}", self._notification_file),
                web.get("/v1/history", self._history),
                web.get("/v1/history/compression", self._compression_history),
                web.get("/v1/history/perceptions/{event_id}/stages", self._perception_stages),
                web.get("/v1/history/perception/summary", self._perception_summary),
                web.get("/v1/history/timeline/pending", self._pending_timeline),
                web.get("/v1/history/timeline/{message_id}/sources", self._timeline_sources),
                web.get("/v1/history/context/preview", self._context_preview),
                web.get("/v1/history/compression/sources/{source_id}", self._compression_history),
                web.get("/v1/history/compression/{batch_id}", self._compression_history),
                web.get("/v1/history/screenshot-groups/{group_id}", self._history),
                web.get("/v1/history/files/{id}", self._history_file),
                web.get("/v1/outputs", self._outputs),
                web.post("/v1/outputs/{output_id}/ack", self._ack),
            ]
        )
        self._app = app
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _terminal_tools_lifetime(self, app: web.Application) -> AsyncIterator[None]:
        async def expire_pending() -> None:
            while True:
                try:
                    self.gateway.store.remote_tools.expire()
                except sqlite3.OperationalError as exc:
                    logger.warning("终端工具过期检查失败", extra={"error_type": type(exc).__name__})
                await asyncio.sleep(1)

        task = asyncio.create_task(expire_pending())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _tool_binding(self, request: web.Request) -> HTTPTerminalSettings:
        binding = self._authenticate(request)
        if not _key_transport_allowed(request):
            raise _HTTPError(403, "secure_transport_required")
        if request.query:
            raise _HTTPError(400, "invalid_query")
        # A shared credential cannot prove which physical client claimed a command.
        token = self._tokens[binding.terminal_id]
        if sum(hmac.compare_digest(t, token) for t in self._tokens.values()) != 1:
            raise _HTTPError(403, "unique_terminal_credential_required")
        return binding

    async def _tool_claim(self, request: web.Request) -> web.Response:
        binding = self._tool_binding(request)
        data = await _read_input(request, ClaimInput, "invalid_terminal_tool_claim")
        self.terminal_capabilities.update(binding.terminal_id, data.tools)
        call = (
            self.gateway.store.remote_tools.claim(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                terminal_id=binding.terminal_id,
                tools=data.tools,
            )
            if data.tools
            else None
        )
        return web.json_response({"call": call.model_dump(mode="json") if call else None})

    async def _tool_result(self, request: web.Request) -> web.Response:
        binding = self._tool_binding(request)
        data = await _read_input(request, ResultInput, "invalid_terminal_tool_result")
        try:
            call = self.gateway.store.remote_tools.complete(
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
            raise _HTTPError(404, "not_found") from None
        except RemoteCallConflict:
            raise _HTTPError(409, "terminal_tool_conflict") from None
        except ValueError:
            raise _HTTPError(400, "invalid_terminal_tool_result") from None
        return web.json_response({"id": call.id, "status": call.status})

    async def _tool_history(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        query = request.query
        if set(query) - {"cursor", "limit"} or any(len(query.getall(k)) != 1 for k in query):
            raise _HTTPError(400, "invalid_query")
        try:
            items, cursor = self.gateway.store.remote_tools.list(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                limit=int(query.get("limit", "30")),
                cursor=query.get("cursor"),
            )
        except ValueError:
            raise _HTTPError(400, "invalid_query") from None
        return web.json_response(
            {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor}
        )

    async def _manage_api_keys(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if not binding.manage_api_keys:
            raise _HTTPError(403, "api_key_management_forbidden")
        if not _key_transport_allowed(request):
            raise _HTTPError(403, "secure_transport_required")
        if request.query:
            raise _HTTPError(400, "invalid_query")
        if self._api_keys is None:
            raise _HTTPError(503, "api_key_management_unavailable")
        try:
            if request.method == "POST":
                data = await _read_input(request, _APIKeyInput, "invalid_request")
                items = await asyncio.to_thread(self._api_keys.update, data.id, data.api_key)
            else:
                items = await asyncio.to_thread(self._api_keys.list_items)
        except KeyError:
            raise _HTTPError(404, "unknown_key_id") from None
        except ValueError:
            raise _HTTPError(400, "invalid_api_key") from None
        except OSError:
            raise _HTTPError(503, "api_key_management_unavailable") from None
        return web.json_response({"items": items})

    async def _profile(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if request.query:
            raise _HTTPError(400, "invalid_query")
        path = self._profile_files.get(binding.user_id)
        document: ProfileDocument = {"status": "unconfigured", "markdown": "", "effective": False}
        if path is not None:
            store = self.gateway.store
            today = store.now_utc().astimezone(ZoneInfo(store.timeline_policy.timezone)).date()
            try:
                document = await asyncio.to_thread(read_profile_document, path, today=today)
            except (OSError, ValueError):
                raise _HTTPError(503, "profile_unavailable") from None
        return web.json_response(document, headers={"Cache-Control": "private, no-store"})

    async def _notifications(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        query = request.query
        if set(query) - {"cursor", "limit", "pending", "unread"} or any(
            len(query.getall(key)) != 1 for key in query
        ):
            raise _HTTPError(400, "invalid_query")
        pending, unread = query.get("pending", "0"), query.get("unread", "0")
        if pending not in {"0", "1"} or unread not in {"0", "1"} or pending == unread == "1":
            raise _HTTPError(400, "invalid_query")
        try:
            items, cursor = self.gateway.store.list_user_notifications(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                terminal_id=binding.terminal_id,
                cursor=query.get("cursor"),
                limit=int(query.get("limit", "50")),
                pending_only=pending == "1",
                unread_only=unread == "1",
            )
        except ValueError:
            raise _HTTPError(400, "invalid_query") from None
        return web.json_response(
            {"items": items, "next_cursor": cursor}, headers={"Cache-Control": "private, no-store"}
        )

    async def _notification_ack(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        try:
            item = self.gateway.store.ack_user_notification(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                terminal_id=binding.terminal_id,
                notification_id=request.match_info["id"],
                read=request.match_info["state"] == "read",
            )
        except LookupError:
            raise _HTTPError(404, "not_found") from None
        return web.json_response(item, headers={"Cache-Control": "private, no-store"})

    async def _notification_file(self, request: web.Request) -> web.StreamResponse:
        binding = self._authenticate(request)
        store = self.gateway.store
        try:
            file, rowid = store.notification_file(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                terminal_id=binding.terminal_id,
                notification_id=request.match_info["id"],
                file_id=request.match_info["file_id"],
            )
        except LookupError:
            raise _HTTPError(404, "not_found") from None
        size = file["size_bytes"]
        response = web.StreamResponse(
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(file["name"], safe=""),
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
                    store.read_notification_chunk(rowid, offset, min(size - offset, 64 * 1024))
                )
        await response.write_eof()
        return response

    async def _schedule(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        query = request.query
        allowed = {"start", "end", "limit", "cursor"}
        if not {"start", "end"} <= set(query) <= allowed or any(
            len(query.getall(key)) != 1 for key in query
        ):
            raise _HTTPError(400, "invalid_query")
        try:
            items, cursor = self.gateway.store.list_schedules(
                user_id=binding.user_id,
                conversation_id=binding.conversation_id,
                start=datetime.fromisoformat(query["start"]),
                end=datetime.fromisoformat(query["end"]),
                limit=int(query.get("limit", "100")),
                cursor=query.get("cursor"),
            )
        except (ValueError, OverflowError):
            raise _HTTPError(400, "invalid_query") from None
        return web.json_response(
            {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor},
            headers={"Cache-Control": "private, no-store"},
        )

    async def _history_file(self, request: web.Request) -> web.FileResponse:
        return self.history_files.download(
            self.gateway.store, self._authenticate(request), request.match_info["id"]
        )

    async def _perception_stages(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if request.query:
            raise _HTTPError(400, "invalid_query")
        try:
            result = self.gateway.store.perception_stages(
                binding.user_id,
                binding.conversation_id,
                request.match_info["event_id"],
                ai_enabled=self.timeline_ai_enabled,
            )
        except LookupError:
            raise _HTTPError(404, "not_found") from None
        return web.json_response(result, headers={"Cache-Control": "private, no-store"})

    async def _pending_timeline(self, request: web.Request) -> web.Response:
        """Preview the activity message the next run would read, without acknowledging it."""
        binding = self._authenticate(request)
        if request.query:
            raise _HTTPError(400, "invalid_query")
        store = self.gateway.store
        message = store.pending_timeline(binding.conversation_id, binding.user_id)
        return web.json_response(
            {
                "message": None
                if message is None
                else {
                    "text": message.text,
                    "timestamp": message.timestamp,
                    "origin": message.origin,
                    "bytes": estimate(message.text),
                },
                "budget": store.timeline_policy.delivery_tokens,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _timeline_sources(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if request.query:
            raise _HTTPError(400, "invalid_query")
        try:
            result = self.gateway.store.timeline_sources(
                binding.user_id,
                binding.conversation_id,
                request.match_info["message_id"],
            )
        except LookupError:
            raise _HTTPError(404, "not_found") from None
        except ValueError:
            raise _HTTPError(400, "invalid_query") from None
        return web.json_response(result, headers={"Cache-Control": "private, no-store"})

    async def _perception_summary(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        query = request.query
        if set(query) - {"from", "to"} or any(len(query.getall(k)) != 1 for k in query):
            raise _HTTPError(400, "invalid_query")

        def parse_bound(name: str) -> float | None:
            raw = query.get(name)
            if raw is None:
                return None
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                raise ValueError("Timezone required")
            return dt.timestamp()

        try:
            start = parse_bound("from")
            end = parse_bound("to")
            if start is not None and end is not None and start > end:
                raise ValueError("Invalid range")
        except (ValueError, OverflowError):
            raise _HTTPError(400, "invalid_query") from None
        result = self.gateway.store.perception_summary(
            binding.user_id,
            binding.conversation_id,
            start=start,
            end=end,
        )
        return web.json_response(result, headers={"Cache-Control": "private, no-store"})

    async def _context_preview(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if request.query:
            raise _HTTPError(400, "invalid_query")
        data = self.gateway.store.context_preview_data(binding.conversation_id, binding.user_id)
        profile_prompt = ""
        profile_path = self._profile_files.get(binding.user_id)
        if profile_path is not None:
            today = data["now"].astimezone(ZoneInfo(data["timezone"])).date()
            profile_prompt = await asyncio.to_thread(read_profile_prompt, profile_path, today=today)

        def approx(byte_count: int) -> int:
            return 0 if byte_count == 0 else max(1, round(byte_count / 3))

        base_bytes = estimate(self._system_prompt)
        profile_bytes = estimate(profile_prompt)
        system_bytes = base_bytes + profile_bytes

        history = data["history"]
        checkpoint = data["checkpoint"]
        summary_text = ""
        cut = 0
        compactions = 0
        if (
            checkpoint is not None
            and 0 < checkpoint.summarized_messages <= len(history)
            and history_digest(history[: checkpoint.summarized_messages])
            == checkpoint.prefix_digest
        ):
            summary_text = checkpoint.summary
            cut = checkpoint.summarized_messages
            compactions = checkpoint.compactions

        recent_items = []
        recent_bytes = 0
        for msg in history[cut:]:
            if isinstance(msg, UserMessage):
                role = "user"
                origin = msg.origin or ("活动时间线" if msg.source == "timeline" else "用户消息")
                timestamp = msg.timestamp
                text = msg.text
            elif isinstance(msg, AssistantMessage):
                role = "assistant"
                origin = "主 Agent"
                timestamp = None
                parts = [msg.text] if msg.text else []
                for call in msg.tool_calls:
                    args = json.dumps(call.arguments, ensure_ascii=False)
                    parts.append(f"{call.name}({args})")
                text = "\n".join(parts)
            elif isinstance(msg, ToolResultMessage):
                role = "tool"
                origin = f"工具 · {msg.name}"
                timestamp = None
                text = msg.content
            else:
                role = "unknown"
                origin = ""
                timestamp = None
                text = str(asdict(msg))
            item_bytes = estimate(text)
            recent_bytes += item_bytes
            recent_items.append(
                {
                    "role": role,
                    "origin": origin,
                    "timestamp": timestamp,
                    "bytes": item_bytes,
                    "tokens_approx": approx(item_bytes),
                    "text": text[:240] + ("…" if len(text) > 240 else ""),
                }
            )

        summary_bytes = estimate(summary_text)
        pending = data["pending_timeline"]
        pending_text = pending.text if pending is not None else ""
        pending_bytes = estimate(pending_text)
        total_bytes = system_bytes + summary_bytes + recent_bytes + pending_bytes

        return web.json_response(
            {
                "segments": [
                    {
                        "kind": "system",
                        "label": "系统提示词（含用户档案）",
                        "bytes": system_bytes,
                        "tokens_approx": approx(system_bytes),
                        "base_bytes": base_bytes,
                        "profile_bytes": profile_bytes,
                        "profile_present": bool(profile_prompt),
                        "preview": profile_prompt[:400] + ("…" if len(profile_prompt) > 400 else "")
                        if profile_prompt
                        else (
                            self._system_prompt[:400]
                            + ("…" if len(self._system_prompt) > 400 else "")
                        ),
                    },
                    {
                        "kind": "session_summary",
                        "label": "会话摘要",
                        "bytes": summary_bytes,
                        "tokens_approx": approx(summary_bytes),
                        "summarized_messages": cut,
                        "compactions": compactions,
                        "text": summary_text,
                    },
                    {
                        "kind": "recent_messages",
                        "label": "最近消息",
                        "bytes": recent_bytes,
                        "tokens_approx": approx(recent_bytes),
                        "message_count": len(recent_items),
                        "items": recent_items,
                    },
                    {
                        "kind": "pending_timeline",
                        "label": "待带入时间线",
                        "bytes": pending_bytes,
                        "tokens_approx": approx(pending_bytes),
                        "budget": data["delivery_budget"],
                        "timestamp": pending.timestamp if pending is not None else None,
                        "text": pending_text,
                    },
                ],
                "total_bytes": total_bytes,
                "total_tokens_approx": approx(total_bytes),
                "input_limit": self._context_input_limit,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _compression_history(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        store = self.gateway.store
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
            raise _HTTPError(400, "invalid_query")
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
                            "files": self.history_files.describe(event),
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
                    ai_enabled=self.timeline_ai_enabled,
                )
            else:
                page = store.browse_compression_batches(
                    user_id=binding.user_id,
                    conversation_id=binding.conversation_id,
                    layer=query.get("layer", ""),
                    limit=limit,
                    cursor=cursor,
                    ai_enabled=self.timeline_ai_enabled,
                )
        except LookupError:
            raise _HTTPError(404, "not_found") from None
        except ValueError:
            raise _HTTPError(400, "invalid_query") from None
        return web.json_response(page, headers={"Cache-Control": "private, no-store"})

    async def _history(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        query = request.query
        group_id = request.match_info.get("group_id")
        allowed = (
            {"cursor", "limit"}
            if group_id
            else {"view", "cursor", "limit", "grouped", "destination", "from", "to"}
        )
        if set(query) - allowed or any(len(query.getall(k)) != 1 for k in query):
            raise _HTTPError(400, "invalid_query")
        try:
            limit = int(query.get("limit", "50"))
            if group_id:
                page, cursor = self.gateway.store.browse_screenshot_group(
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

                def parse_bound(name: str) -> float | None:
                    raw = query.get(name)
                    if raw is None:
                        return None
                    dt = datetime.fromisoformat(raw)
                    if dt.tzinfo is None:
                        raise ValueError("Timezone required")
                    return dt.timestamp()

                page, cursor = self.gateway.store.browse_history(
                    user_id=binding.user_id,
                    conversation_id=binding.conversation_id,
                    view=query.get("view", ""),
                    cursor=query.get("cursor"),
                    limit=limit,
                    grouped=grouped == "1",
                    destination=query.get("destination"),
                    start=parse_bound("from"),
                    end=parse_bound("to"),
                )
        except LookupError:
            raise _HTTPError(404, "not_found") from None
        except ValueError:
            raise _HTTPError(400, "invalid_query") from None
        items = [
            {
                "id": item.id,
                "role": item.role,
                "source": item.source,
                "occurred_at": item.occurred_at,
                "text": item.text,
                "files": [f for event in item.media for f in self.history_files.describe(event)],
                **({"raw": item.raw.model_dump(mode="json")} if item.raw is not None else {}),
                **({"filter_result": item.filter_result} if item.filter_result else {}),
                **({"destination": item.destination} if item.destination else {}),
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

    def _slice_binding(self, request: web.Request) -> HTTPTerminalSettings:
        binding = self._authenticate(request)
        if binding.is_desktop:
            raise _HTTPError(410, "desktop_slice_removed")
        return binding

    async def _audio_submit(self, request: web.Request) -> web.Response:
        return await self.audio.submit(request, self._slice_binding(request))

    async def _image_submit(self, request: web.Request) -> web.Response:
        return await self.images.submit(request, self._slice_binding(request))

    async def _desktop_window(self, request: web.Request) -> web.Response:
        return await self.desktop_windows.submit(request, self._authenticate(request))

    async def _media_status(self, request: web.Request) -> web.Response:
        binding = self._slice_binding(request)
        event_id = request.match_info["event_id"]
        try:
            event = self.gateway.store.get_event(event_id, user_id=binding.user_id)
        except EventError:
            raise _HTTPError(404, "not_found") from None
        expected = "slice.audio" if request.path.startswith("/v1/audio/") else "slice.image"
        if (
            event.type != expected
            or event.terminal_id != binding.terminal_id
            or event.conversation_id != binding.conversation_id
        ):
            raise _HTTPError(404, "not_found")
        status = self.gateway.store.get_slice_status(event_id, user_id=binding.user_id)
        return web.json_response(status.model_dump(mode="json"))

    async def _submit(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        message = await _read_input(request, _MessageInput, "invalid_message")
        with log_context(
            terminal_id=binding.terminal_id,
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
        ):
            try:
                receipt = await self.gateway.conversation.connect(
                    binding.terminal_id, binding.conversation_id
                ).send(message.text, producer_event_id=message.message_id)
            except DuplicateEventError:
                raise _HTTPError(409, "message_id_conflict") from None
            except EventError:
                raise _HTTPError(400, "event_rejected") from None
        return web.json_response(
            {
                "event_id": receipt.event.event_id,
                "job_id": receipt.job_id,
                "duplicate": receipt.duplicate,
            },
            status=202,
        )

    async def _perception(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        data = await _read_input(request, _PerceptionInput, "invalid_perception")
        if data.state_key in {"desktop.activity", "browser.page"} and binding.is_desktop:
            raise _HTTPError(410, "desktop_observation_removed")
        return await self._activity_receipt(self._observe(binding, data))

    async def _observe(self, binding: HTTPTerminalSettings, data: _PerceptionInput) -> Receipt:
        endpoint = self.gateway.perception.connect(
            binding.terminal_id, conversation_id=binding.conversation_id
        )
        try:
            return await endpoint.observe(
                data.state_key,
                data.value,
                producer_event_id=data.event_id,
                occurred_at=data.occurred_at,
            )
        except DuplicateEventError:
            if data.state_key != WINDOW_SPAN_STATE_KEY:
                raise
            old = self.gateway.store.received_event(binding.terminal_id, data.event_id)
            if (
                old is None
                or old.source_id != "default"
                or old.conversation_id != binding.conversation_id
                or old.user_id != binding.user_id
                or old.occurred_at != data.occurred_at
            ):
                raise
            # Screenshot consent can be revoked after the compound request was accepted
            # but before its ACK arrived. A retry without frames still names the same dwell.
            if old.type == "state.observed" and (old.correlation_id or "").startswith(
                DIGEST_PREFIX
            ):
                value = old.payload.get("value")
                if old.payload.get("state_key") != data.state_key or not isinstance(value, dict):
                    raise
                base = {k: v for k, v in value.items() if k != "visual_observations"}
                if json.dumps(data.value, sort_keys=True) != json.dumps(base, sort_keys=True):
                    raise
                return Receipt(event=old, duplicate=True)
            raise

    async def _text_slice(self, request: web.Request) -> web.Response:
        binding = self._slice_binding(request)
        data = await _read_input(request, _SliceInput, "invalid_slice")
        endpoint = self.gateway.slice.connect(
            binding.terminal_id, conversation_id=binding.conversation_id
        )
        return await self._activity_receipt(
            endpoint.record(
                data.text, producer_event_id=data.event_id, occurred_at=data.occurred_at
            )
        )

    @staticmethod
    async def _activity_receipt(submission: Awaitable[Receipt]) -> web.Response:
        try:
            receipt = await submission
        except DuplicateEventError:
            raise _HTTPError(409, "event_id_conflict") from None
        except (ValueError, OverflowError):
            raise _HTTPError(400, "event_rejected") from None
        return web.json_response(
            {"event_id": receipt.event.event_id, "duplicate": receipt.duplicate}, status=202
        )

    async def _job(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        try:
            job = self.gateway.store.get_job(request.match_info["job_id"], user_id=binding.user_id)
            event = self.gateway.store.get_event(job.event_id, user_id=binding.user_id)
        except EventError:
            raise _HTTPError(404, "not_found") from None
        if job.conversation_id != binding.conversation_id or binding.terminal_id not in (
            event.terminal_id,
            event.reply_terminal_id,
        ):
            raise _HTTPError(404, "not_found")
        outputs = self.gateway.store.list_outputs(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            terminal_id=binding.terminal_id,
            job_id=job.job_id,
        )
        return web.json_response(
            {
                "job_id": job.job_id,
                "event_id": job.event_id,
                "conversation_id": job.conversation_id,
                "layer": job.layer,
                "status": job.status,
                "error": "agent_run_failed" if job.error is not None else None,
                "outputs": [_output_data(output) for output in outputs],
            }
        )

    async def _outputs(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if set(request.query) - {"limit"} or len(request.query.getall("limit", [])) > 1:
            raise _HTTPError(400, "invalid_query")
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            raise _HTTPError(400, "invalid_limit") from None
        if not 1 <= limit <= 1000:
            raise _HTTPError(400, "invalid_limit")
        outputs = self.gateway.store.list_outputs(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            terminal_id=binding.terminal_id,
            status="pending",
            limit=limit,
        )
        return web.json_response({"outputs": [_output_data(output) for output in outputs]})

    async def _ack(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        try:
            output = self.gateway.store.get_output(
                request.match_info["output_id"], user_id=binding.user_id
            )
        except EventError:
            raise _HTTPError(404, "not_found") from None
        if (
            output.conversation_id != binding.conversation_id
            or output.terminal_id != binding.terminal_id
        ):
            raise _HTTPError(404, "not_found")
        self.gateway.store.mark_output_delivered(output.output_id)
        return web.json_response({"output_id": output.output_id, "status": "delivered"})

    @property
    def addresses(self) -> tuple[tuple[str, int], ...]:
        """实际监听地址；端口配置为 0 时可读取系统分配的端口。"""
        if self._runner is None:
            return ()
        return tuple((str(address[0]), int(address[1])) for address in self._runner.addresses)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("HTTP interface is closed")
            if self._runner is not None:
                return
            runner = web.AppRunner(self.create_app(), access_log=None, shutdown_timeout=10)
            try:
                await runner.setup()
                await web.TCPSite(runner, self.settings.host, self.settings.port).start()
            except BaseException:
                await runner.cleanup()
                raise
            self._runner = runner
            logger.info("HTTP 交流接口已启动", extra={"addresses": self.addresses})

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(), name="wintermute-http-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        async with self._lifecycle_lock:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
                logger.info("HTTP 交流接口已关闭")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
