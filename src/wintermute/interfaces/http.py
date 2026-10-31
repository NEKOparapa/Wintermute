"""带终端认证的异步 HTTP 交流入口和持久化输出收件箱。"""

import asyncio
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Self

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from wintermute.config.models import AudioSliceSettings, HTTPSettings, HTTPTerminalSettings
from wintermute.events import (
    DuplicateEventError,
    EventError,
    EventGateway,
    OutputRecord,
    Terminal,
)
from wintermute.interfaces.audio import AudioUploadError, AudioUploads
from wintermute.logging import get_logger, log_context

logger = get_logger(__name__)


class _MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    message_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1)

    @field_validator("message_id", "text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field must not be blank")
        return value


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


def _output_data(output: OutputRecord) -> dict[str, Any]:
    # 投递异常可能包含外部请求细节；对外仅提供结果和投递状态。
    return output.model_dump(mode="json", exclude={"error", "user_id"})


class HTTPInterface:
    """将认证终端映射到固定用户和会话，模型运行由现有调度器负责。

    POST 只持久化消息并返回任务 ID。GET 不确认接收，客户端完成处理后
    显式调用 ack；因此断线、重启和通知结果都使用同一个持久化收件箱。
    """

    def __init__(
        self,
        settings: HTTPSettings,
        gateway: EventGateway,
        *,
        environ: Mapping[str, str] | None = None,
        audio_settings: AudioSliceSettings | None = None,
        base_dir: str | Path = ".",
    ) -> None:
        self.settings = settings.model_copy(deep=True)
        self.gateway = gateway
        self.audio = AudioUploads(
            audio_settings or AudioSliceSettings(), gateway, base_dir=base_dir
        )
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
            try:
                terminal = self.gateway.store.get_terminal(binding.terminal_id)
            except EventError:
                registrations.append(
                    Terminal(
                        terminal_id=binding.terminal_id,
                        user_id=binding.user_id,
                        allowed_layers=("conversation", "slice")
                        if self.audio.settings.enabled
                        else ("conversation",),
                    )
                )
            else:
                if (
                    terminal.user_id != binding.user_id
                    or "conversation" not in terminal.allowed_layers
                ):
                    raise ValueError("HTTP terminal conflicts with an existing registration")
                if self.audio.settings.enabled and "slice" not in terminal.allowed_layers:
                    registrations.append(
                        terminal.model_copy(
                            update={
                                "allowed_layers": (*terminal.allowed_layers, "slice"),
                            }
                        )
                    )
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

        app = web.Application(
            client_max_size=self.settings.max_body_bytes,
            middlewares=[errors],
        )
        app.add_routes(
            [
                web.get("/health", self._health),
                web.post("/v1/messages", self._submit),
                web.post("/v1/audio/slices", self._audio_submit),
                web.get("/v1/audio/slices/{event_id}", self._audio_status),
                web.get("/v1/jobs/{job_id}", self._job),
                web.get("/v1/outputs", self._outputs),
                web.post("/v1/outputs/{output_id}/ack", self._ack),
            ]
        )
        self._app = app
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _audio_submit(self, request: web.Request) -> web.Response:
        return await self.audio.submit(request, self._authenticate(request))

    async def _audio_status(self, request: web.Request) -> web.Response:
        return await self.audio.status(request, self._authenticate(request))

    async def _submit(self, request: web.Request) -> web.Response:
        binding = self._authenticate(request)
        if request.content_type != "application/json":
            raise _HTTPError(415, "json_required")
        try:
            body = json.loads(
                await request.text(),
                object_pairs_hook=_json_pairs,
                parse_constant=_reject_constant,
            )
            message = _MessageInput.model_validate(body)
        except (ValueError, UnicodeError, RecursionError, ValidationError):
            raise _HTTPError(400, "invalid_message") from None
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
