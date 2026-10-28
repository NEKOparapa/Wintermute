"""将配置中的交流接口与主会话、后台任务和事件调度连接起来。"""

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Self

from wintermute.agent import AgentRunner
from wintermute.agent.tools import BackgroundSubagentManager, BackgroundTaskState
from wintermute.config import AppConfig, ConfigError
from wintermute.config.factory import (
    ModelSet,
    create_background_manager,
    create_runner,
    create_scheduler,
)
from wintermute.events import (
    BackgroundEventPublisher,
    EventError,
    EventGateway,
    RuntimeEvent,
    SQLiteStore,
    Terminal,
)
from wintermute.logging import get_logger

if TYPE_CHECKING:
    from wintermute.interfaces.http import HTTPInterface
    from wintermute.interfaces.telegram import TelegramInterface

logger = get_logger(__name__)


class ConversationService:
    """共享同一调度器；每个会话拥有独立 Runner 和后台任务管理器。

    模型客户端和事件存储由调用方持有。后台结果沿启动任务时的终端回复，
    不会被后来从另一终端进入的交流改写目标。关闭后不能重启此实例。
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
        self.base_dir = Path(base_dir).resolve()
        self.gateway = EventGateway(store)
        self.scheduler = create_scheduler(
            config.events, self.gateway, self._make_runner, slice_model=models.main
        )
        self.scheduler.subscribe(self._observe)
        self.http: HTTPInterface | None = None
        self.telegram: TelegramInterface | None = None
        self._backgrounds: dict[str, BackgroundSubagentManager] = {}
        self._origin_terminal: ContextVar[str | None] = ContextVar(
            f"wintermute-reply-terminal-{id(self)}", default=None
        )
        self._stack: AsyncExitStack | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    def _observe(self, update: RuntimeEvent) -> None:
        if update.event.type == "agent_start":
            event = self.gateway.store.get_event(update.event_id)
            # 回调在主 Runner 任务上下文中执行，后台任务及其完成回调继承此值。
            self._origin_terminal.set(event.reply_terminal_id)

    def _make_runner(self, conversation_id: str) -> AgentRunner:
        if self._closed:
            raise RuntimeError("Conversation service is closing")
        background = self._backgrounds.get(conversation_id)
        if background is None and self.config.subagents:
            user_id = self.gateway.store.get_conversation_user(conversation_id)
            terminal_id = "wintermute:internal:" + sha256(conversation_id.encode()).hexdigest()
            try:
                terminal = self.gateway.store.get_terminal(terminal_id)
            except EventError:
                self.gateway.register_terminal(
                    Terminal(
                        terminal_id=terminal_id, user_id=user_id, allowed_layers=("notification",)
                    )
                )
            else:
                if terminal.user_id != user_id or "notification" not in terminal.allowed_layers:
                    raise ValueError("Internal notification terminal conflicts with stored data")

            async def publish(state: BackgroundTaskState) -> None:
                endpoint = self.gateway.notification.connect(
                    terminal_id,
                    conversation_id,
                    source_id="background_subagent",
                    reply_terminal_id=self._origin_terminal.get(),
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
            self.models.main,
            base_dir=self.base_dir,
            additional_tools=background.tools if background is not None else (),
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
                # 所有接口先构造并验证凭据，再开始监听或领取远端消息。
                if self.config.interfaces.http.enabled:
                    for binding in self.config.interfaces.http.terminals:
                        binding.resolve_token()
                if self.config.interfaces.telegram.enabled:
                    self.config.interfaces.telegram.resolve_bot_token()
                if self.config.interfaces.http.enabled:
                    from wintermute.interfaces.http import HTTPInterface

                    self.http = HTTPInterface(self.config.interfaces.http, self.gateway)
                if self.config.interfaces.telegram.enabled:
                    from wintermute.interfaces.telegram import TelegramInterface

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
                self._close(), name="wintermute-interfaces-close"
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
