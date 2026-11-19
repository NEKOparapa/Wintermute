"""带设备认证的 HTTP 交流、设备采集入口和持久化输出收件箱。"""

import asyncio
import base64
import binascii
import hmac
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Self

from aiohttp import web

from wintermute.config.models import (
    AudioSliceSettings,
    HTTPDeviceSettings,
    HTTPSettings,
    ImageSliceSettings,
)
from wintermute.events.core import Device, EventError, Layer
from wintermute.events.ingress import EventGateway
from wintermute.events.integrations.connectors import ConnectorManager
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.slices.asr import DeepSeekASR
from wintermute.events.slices.image import ImageSliceProcessor
from wintermute.events.storage.repositories.devices import DeviceIdentityConflict
from wintermute.interfaces.audio import AudioUploadError, AudioUploads
from wintermute.interfaces.desktop_window import DesktopWindowUploads
from wintermute.interfaces.history_files import HistoryFiles
from wintermute.interfaces.http_routes import (
    capture,
    chat_documents,
    chat_media,
    connectors,
    conversation,
    history,
    memory,
    notifications,
    questions,
    records,
    schedule,
    settings,
    terminal_tools,
)
from wintermute.interfaces.http_routes.common import HTTP_INTERFACE, HTTPError
from wintermute.interfaces.http_routes.common import key_transport_allowed as _key_transport_allowed
from wintermute.interfaces.http_routes.terminal_tools import ClaimInput as ClaimInput
from wintermute.interfaces.http_routes.terminal_tools import ResultInput as ResultInput
from wintermute.interfaces.image import ImageUploads
from wintermute.logging import get_logger
from wintermute.memory import MemoryVault

if TYPE_CHECKING:
    from wintermute.config.api_keys import APIKeyRegistry

logger = get_logger(__name__)


class HTTPInterface:
    """将认证设备映射到固定用户和会话，模型运行由现有调度器负责。

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
        memory_vaults: Mapping[str, MemoryVault] | None = None,
        base_dir: str | Path = ".",
    ) -> None:
        self.settings = settings.model_copy(deep=True)
        self.gateway = gateway
        self.api_keys = api_keys
        self.device_capabilities = DeviceCapabilities()
        self.connectors = ConnectorManager()
        self.images = ImageUploads(image_settings or ImageSliceSettings(), gateway, base_dir)
        self.desktop_windows = DesktopWindowUploads(
            gateway,
            self.images.directory,
            desktop_vision.model if desktop_vision else None,
            timeout=desktop_vision.timeout if desktop_vision else 60,
        )
        audio = audio_settings or AudioSliceSettings()
        self.audio = AudioUploads(audio, gateway, base_dir=base_dir)
        self.asr_key = api_keys.provider("asr") if api_keys else audio.asr.resolve_api_key
        self.chat_asr = DeepSeekASR(
            endpoint=audio.asr.endpoint,
            timeout=audio.asr.timeout,
            api_key=self.asr_key,
        )
        self.chat_media_slots = asyncio.Semaphore(4)
        self.history_files = HistoryFiles(self.images.directory, self.audio.directory)
        self.timeline_ai_enabled = timeline_ai_enabled
        self.memory_vaults = dict(memory_vaults or {})
        self._bindings = {item.device_id: item for item in self.settings.devices}
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
                raise ValueError("An API key management device requires a unique bearer token")
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._register_bindings()

    def _register_bindings(self) -> None:
        # 先检查全部已有归属，避免后面的错误留下部分注册配置。
        registrations: list[Device] = []
        for binding in self._bindings.values():
            http_layers: tuple[Layer, ...] = ("conversation", "perception", "record")
            if not binding.is_desktop:
                http_layers += ("slice",)
            try:
                device = self.gateway.store.get_device(binding.device_id)
            except EventError:
                registrations.append(
                    Device(
                        device_id=binding.device_id,
                        user_id=binding.user_id,
                        allowed_layers=http_layers,
                    )
                )
            else:
                if (
                    device.user_id != binding.user_id
                    or "conversation" not in device.allowed_layers
                ):
                    raise ValueError("HTTP device conflicts with an existing registration")
                allowed = tuple(dict.fromkeys((*device.allowed_layers, *http_layers)))
                if binding.is_desktop:
                    allowed = tuple(layer for layer in allowed if layer != "slice")
                if allowed != device.allowed_layers:
                    registrations.append(device.model_copy(update={"allowed_layers": allowed}))
            try:
                owner = self.gateway.store.get_conversation_user(binding.conversation_id)
            except EventError:
                continue
            if owner != binding.user_id:
                raise ValueError("HTTP conversation conflicts with an existing registration")
        for device in registrations:
            self.gateway.register_device(device)
        for binding in self._bindings.values():
            self.gateway.register_conversation(binding.conversation_id, binding.user_id)

    def authenticate(self, request: web.Request) -> HTTPDeviceSettings:
        # Older firmware may still send the previous header during a rolling upgrade.
        ids = request.headers.getall("X-Device-ID", []) + request.headers.getall(
            "X-Terminal-ID", []
        )
        if len(ids) != 1 or len(request.headers.getall("Authorization", [])) != 1:
            raise HTTPError(401, "unauthorized")
        device_id = ids[0]
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        expected = self._tokens.get(device_id)
        if (
            expected is None
            or not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(token.encode("utf-8"), expected)
        ):
            raise HTTPError(401, "unauthorized")
        instances = request.headers.getall("X-Device-Instance", [])
        names = request.headers.getall("X-Device-Name", [])
        if len(instances) > 1 or len(names) > 1 or (names and not instances):
            raise HTTPError(400, "invalid_device_identity")
        instance = instances[0] if instances else None
        if instance is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{12,96}", instance):
            raise HTTPError(400, "invalid_device_identity")
        name = None
        if names:
            try:
                name = base64.b64decode(names[0], validate=True).decode("utf-8").strip()
            except (ValueError, binascii.Error):
                raise HTTPError(400, "invalid_device_name") from None
            if not 1 <= len(name) <= 80 or any(unicodedata.category(c) == "Cc" for c in name):
                raise HTTPError(400, "invalid_device_name")
        try:
            rename = request.method == "POST" and request.path == "/v1/devices/self"
            if rename and name is None:
                raise HTTPError(400, "invalid_device_name")
            self.gateway.store.devices.observe(device_id, instance, name, rename=rename)
        except DeviceIdentityConflict:
            raise HTTPError(409, "device_id_in_use") from None
        self.device_capabilities.observe(device_id)
        return self._bindings[device_id]

    async def _set_device_profile(self, request: web.Request) -> web.Response:
        binding = self.authenticate(request)
        return web.json_response(
            {"device_id": binding.device_id}, headers={"Cache-Control": "no-store"}
        )

    def authenticate_tool(self, request: web.Request) -> HTTPDeviceSettings:
        binding = self.authenticate(request)
        if not _key_transport_allowed(request):
            raise HTTPError(403, "secure_transport_required")
        if request.query:
            raise HTTPError(400, "invalid_query")
        # A shared credential cannot prove which physical client claimed a command.
        token = self._tokens[binding.device_id]
        if sum(hmac.compare_digest(t, token) for t in self._tokens.values()) != 1:
            raise HTTPError(403, "unique_device_credential_required")
        return binding

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
            except (HTTPError, AudioUploadError) as exc:
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
                (
                    "/v1/terminal-tools/",
                    "/v1/memory/",
                    "/v1/records/",
                    "/v1/questions",
                    "/v1/connectors",
                )
            ):
                response.headers["Cache-Control"] = "private, no-store"
            return response

        app = web.Application(
            client_max_size=self.settings.max_body_bytes,
            middlewares=[private_settings, errors],
        )
        app[HTTP_INTERFACE] = self
        app.on_startup.append(self.desktop_windows.recover)
        app.cleanup_ctx.append(self.desktop_windows.lifetime)
        app.cleanup_ctx.append(terminal_tools.lifetime)
        app.router.add_get("/health", self._health)
        app.router.add_post("/v1/devices/self", self._set_device_profile)
        for table in (
            conversation.routes,
            chat_documents.routes,
            chat_media.routes,
            capture.routes,
            connectors.routes,
            terminal_tools.routes,
            history.routes,
            notifications.routes,
            questions.routes,
            schedule.routes,
            settings.routes,
            memory.routes,
            records.routes,
        ):
            app.add_routes(table)
        self._app = app
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

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
