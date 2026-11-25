"""将配置中的交流接口与主会话、后台任务和事件调度连接起来。"""

import asyncio
from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from purin.agent import AgentRunner, Tool
from purin.agent.tools import BackgroundSubagentManager, BackgroundTaskState
from purin.agent.tools.calendar.holidays import HolidayCalendar
from purin.agent.tools.devices import DeviceToolBindings
from purin.agent.tools.registry import create_conversation_tools
from purin.agent.tools.web import open_web_tools
from purin.application.agents import create_background_manager, create_runner
from purin.application.models import ConversationModel, ModelSet
from purin.application.runtime import create_image_processor, create_scheduler
from purin.config import AppConfig, ConfigError
from purin.events.core import Device, EventError
from purin.events.ingress import EventGateway
from purin.events.integrations import BackgroundEventPublisher
from purin.events.integrations.connectors import ConnectorManager
from purin.events.storage.sqlite import SQLiteStore
from purin.logging import get_logger

if TYPE_CHECKING:
    from purin.interfaces.http import HTTPInterface
    from purin.interfaces.telegram import TelegramInterface

logger = get_logger(__name__)


class ConversationService:
    """共享同一调度器；每个会话拥有独立 Runner 和后台任务管理器。

    模型客户端和事件存储由调用方持有。后台结果沿启动任务时的设备回复，
    不会被后来从另一设备进入的交流改写目标。关闭后不能重启此实例。
    """

    def __init__(
        self,
        config: AppConfig,
        models: ModelSet,
        store: SQLiteStore,
        *,
        base_dir: str | Path = ".",
    ) -> None:
        if not config.interfaces.http.enabled and not config.interfaces.telegram.enabled:
            raise ConfigError("At least one conversation interface must be enabled")
        self.config = config.model_copy(deep=True)
        self.models = models
        self.api_keys = models.api_keys
        self.base_dir = Path(base_dir).resolve()
        self.gateway = EventGateway(store)
        self.vision = create_image_processor(
            self.config.events.slices, models.image_model, base_dir=self.base_dir
        )
        self.scheduler = create_scheduler(
            config.events,
            self.gateway,
            self._make_runner,
            main_model=models.main,
            image_processor=self.vision,
            timeline_summary_model=models.timeline_summary,
            memory_model=models.memory_model,
            api_keys=self.api_keys,
            base_dir=self.base_dir,
        )
        self.http: HTTPInterface | None = None
        self.telegram: TelegramInterface | None = None
        self._connectors = ConnectorManager()
        self._holidays = HolidayCalendar(store.holiday_years)
        self._web_tools: tuple[Tool[Any], ...] = ()
        self._backgrounds: dict[str, BackgroundSubagentManager] = {}
        self._stack: AsyncExitStack | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    def _make_runner(self, conversation_id: str) -> AgentRunner:
        if self._closed:
            raise RuntimeError("Conversation service is closing")
        user_id = self.gateway.store.get_conversation_user(conversation_id)
        background = self._backgrounds.get(conversation_id)
        if background is None and self.config.subagents:
            device_id = "purin:internal:" + sha256(conversation_id.encode()).hexdigest()
            try:
                device = self.gateway.store.get_device(device_id)
            except EventError:
                self.gateway.register_device(
                    Device(device_id=device_id, user_id=user_id, allowed_layers=("notification",))
                )
            else:
                if device.user_id != user_id or "notification" not in device.allowed_layers:
                    raise ValueError("Internal notification device conflicts with stored data")

            async def publish(state: BackgroundTaskState) -> None:
                endpoint = self.gateway.notification.connect(
                    device_id,
                    conversation_id,
                    source_id="background_subagent",
                    reply_device_id=self.scheduler.reply_device_id,
                )
                await BackgroundEventPublisher(endpoint)(state)

            background = create_background_manager(
                self.config,
                self.models,
                base_dir=self.base_dir,
                notification_publisher=publish,
            )
            self._backgrounds[conversation_id] = background
        return create_runner(
            self.config,
            ConversationModel(
                self.models.main,
                lambda ref: self.gateway.store.chat_image(ref, user_id, conversation_id),
            ),
            base_dir=self.base_dir,
            additional_tools=create_conversation_tools(
                self.gateway.store,
                conversation_id,
                timezone=self.config.events.slices.timezone,
                workspace=self.base_dir / Path(self.config.tools.cwd).expanduser(),
                vault=self.scheduler.memory_vaults.get(user_id),
                allow_character_write=lambda: self.scheduler.memory_character_write_allowed,
                reply_device_id=lambda: self.scheduler.reply_device_id,
                devices=DeviceToolBindings(
                    self.http.devices.active,
                    self.http.device_capabilities,
                    self.http.history_files.resolve,
                )
                if self.http is not None
                else None,
                connectors=self.http.connectors if self.http is not None else self._connectors,
                web_tools=self._web_tools,
                holidays=self._holidays,
                background_tools=background.tools if background is not None else (),
            ),
        )

    async def _close_backgrounds(self) -> None:
        outcomes = await asyncio.gather(
            *(manager.aclose() for manager in self._backgrounds.values()), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                logger.error("后台任务关闭失败", extra={"error_type": type(outcome).__name__})

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Conversation service is closed")
            if self._stack is not None:
                return
            stack = AsyncExitStack()
            try:
                web = self.config.tools.web
                if web.enabled:
                    key = self.api_keys.provider("web") if self.api_keys else web.resolve_api_key
                    self._web_tools = await stack.enter_async_context(open_web_tools(web, key))
                # 所有接口先构造并验证凭据，再开始监听或领取远端消息。
                if self.config.interfaces.telegram.enabled:
                    self.config.interfaces.telegram.resolve_bot_token()
                if self.config.interfaces.http.enabled:
                    from purin.interfaces.http import HTTPInterface

                    slices = self.config.events.slices
                    self.http = HTTPInterface(
                        self.config.interfaces.http,
                        self.gateway,
                        api_keys=self.api_keys,
                        audio_settings=slices.audio,
                        image_settings=slices.image.model_copy(
                            update={"enabled": slices.enabled and slices.image.enabled}
                        ),
                        desktop_vision=self.vision,
                        timeline_ai_enabled=self.scheduler.timeline_worker.model is not None,
                        memory_vaults=self.scheduler.memory_vaults,
                        base_dir=self.base_dir,
                    )
                    self.scheduler.notification_targets = self.http.devices.notification_targets
                if self.config.interfaces.telegram.enabled:
                    from purin.interfaces.telegram import TelegramInterface

                    self.telegram = TelegramInterface(
                        self.config.interfaces.telegram, self.gateway, self.scheduler.output_router
                    )
                # 关闭顺序：网络入口、调度器、后台任务；最后由外层关闭模型和数据库。
                stack.push_async_callback(self._close_backgrounds)
                await stack.enter_async_context(self.scheduler)
                if self.http is not None:
                    await stack.enter_async_context(self.http)
                if self.telegram is not None:
                    await stack.enter_async_context(self.telegram)
            except BaseException:
                self._closed = True
                await stack.aclose()
                raise
            self._stack = stack
            logger.info("交流接口服务已启动")

    async def wait(self) -> None:
        """持续运行直到调度器退出；故障由上层捕获并退出服务上下文。"""
        await self.scheduler.wait_until_stopped()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="purin-interfaces-close"
            )
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
            logger.info("交流接口服务已关闭")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
