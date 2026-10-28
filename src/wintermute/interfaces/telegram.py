"""Telegram 文本交流入口、长轮询接收和持久输出投递。"""

import asyncio
import hashlib
import logging
import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any, Self

from aiohttp import ClientSession, ClientTimeout

from wintermute.config.models import TelegramBindingSettings, TelegramSettings
from wintermute.events import (
    EventError,
    EventGateway,
    OutputRecord,
    OutputRouter,
    Receipt,
    Terminal,
)

logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    """仅包含状态和冷却时间；不携带包含 Bot Token 的请求地址。"""

    def __init__(self, status: int | None = None, *, retry_after: float = 0) -> None:
        self.status = status
        self.retry_after = retry_after
        detail = f" (status={status})" if status is not None else ""
        super().__init__(f"Telegram API request failed{detail}")


def _text_chunks(text: str) -> list[str]:
    """按最多 4096 个 UTF-16 单位拆分，不切断 Unicode 码点。"""
    if not text:
        return ["（本轮没有文本回复）"]
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > 4096:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(character)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


class TelegramInterface:
    """一个 Bot 的多个显式会话绑定，输入与输出均通过既有事件层。

    输入以 update_id 幂等持久化后才确认。输出采用至少一次投递；进程内
    缓存最近 256 条输出的分片进度，避免一般重试重复已成功分片。进程重启、
    缓存淘汰或 Telegram 已发送但网络确认丢失时仍可能重复，不能保证恰好一次。
    不自动删除 Webhook 或清空待处理更新。
    """

    def __init__(
        self,
        settings: TelegramSettings,
        gateway: EventGateway,
        output_router: OutputRouter,
        *,
        environ: Mapping[str, str] | None = None,
        session: ClientSession | None = None,
    ) -> None:
        if output_router.store is not gateway.store:
            raise ValueError("Telegram gateway and output router must share a store")
        self.settings = settings.model_copy(deep=True)
        self.gateway = gateway
        self.output_router = output_router
        self._environ = dict(environ) if environ is not None else None
        self._session = session
        self._owns_session = session is None
        self._token: str | None = None
        self._bindings = {item.chat_id: item for item in self.settings.bindings}
        self._registered = False
        self._disconnectors: list[Callable[[], None]] = []
        self._offset = 0
        self._retry_at = 0.0
        self._poll_lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._progress: OrderedDict[str, tuple[str, int]] = OrderedDict()

    @property
    def offset(self) -> int:
        return self._offset

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Telegram interface is closed")
        if not self.settings.enabled:
            raise ValueError("Telegram interface is disabled")

    def _register_bindings(self) -> None:
        self._ensure_open()
        if self._registered:
            return
        # 先验证已有归属，避免一个冲突绑定覆盖其他终端的权限。
        for binding in self.settings.bindings:
            try:
                terminal = self.gateway.store.get_terminal(binding.terminal_id)
            except EventError:
                pass
            else:
                if terminal.user_id != binding.user_id or terminal.allowed_layers != (
                    "conversation",
                ):
                    raise ValueError("Telegram terminal registration conflicts with existing data")
            try:
                owner = self.gateway.store.get_conversation_user(binding.conversation_id)
            except EventError:
                pass
            else:
                if owner != binding.user_id:
                    raise ValueError("Telegram conversation belongs to another user")
        for binding in self.settings.bindings:
            self.gateway.register_terminal(
                Terminal(
                    terminal_id=binding.terminal_id,
                    user_id=binding.user_id,
                    allowed_layers=("conversation",),
                )
            )
            self.gateway.register_conversation(binding.conversation_id, binding.user_id)
        self._registered = True

    async def _ensure_client(self) -> ClientSession:
        self._ensure_open()
        if self._token is None:
            self._token = self.settings.resolve_bot_token(self._environ)
        if self._session is None:
            self._session = ClientSession(
                timeout=ClientTimeout(total=self.settings.request_timeout),
                raise_for_status=False,
            )
        if self._session.closed:
            raise RuntimeError("Telegram client session is closed")
        return self._session

    async def start(self) -> None:
        self._ensure_open()
        if self._poll_task is not None:
            return
        try:
            # 缺失凭据时先报错，不改变终端注册。
            await self._ensure_client()
            self._register_bindings()
            for binding in self.settings.bindings:
                self._disconnectors.append(
                    self.output_router.connect(
                        binding.terminal_id, partial(self._send_output, binding)
                    )
                )
            self._poll_task = asyncio.create_task(self._poll(), name="wintermute-telegram-poll")
        except BaseException:
            await self.aclose()
            raise
        logger.info("Telegram 交流接口已启动", extra={"terminals": len(self.settings.bindings)})

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _request(self, method: str, payload: dict[str, Any]) -> Any:
        session = await self._ensure_client()
        loop = asyncio.get_running_loop()
        remaining = self._retry_at - loop.time()
        if remaining > 0:
            raise TelegramAPIError(429, retry_after=remaining)
        url = f"{self.settings.api_base.rstrip('/')}/bot{self._token}/{method}"
        status: int | None = None
        try:
            async with session.post(
                url,
                json=payload,
                allow_redirects=False,
                timeout=ClientTimeout(total=self.settings.request_timeout),
            ) as response:
                status = response.status
                data = await response.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            # aiohttp 异常通常含 URL；禁止它进入持久输出错误和日志。
            raise TelegramAPIError(status) from None
        if not isinstance(data, dict) or data.get("ok") is not True or status != 200:
            retry_after = 0.0
            if isinstance(data, dict):
                code = data.get("error_code")
                if type(code) is int:
                    status = code
                parameters = data.get("parameters")
                if status == 429 and isinstance(parameters, dict):
                    delay = parameters.get("retry_after")
                    if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                        try:
                            seconds = float(delay)
                        except OverflowError:
                            seconds = 86400.0
                        if math.isfinite(seconds) and seconds > 0:
                            retry_after = seconds
                            self._retry_at = loop.time() + retry_after
            raise TelegramAPIError(status, retry_after=retry_after)
        return data.get("result")

    async def process_update(self, update: dict[str, Any]) -> Receipt | None:
        """接收已取得的更新；不自行推进游标，持久化异常交由轮询重试。"""
        self._register_bindings()
        update_id = update.get("update_id")
        message = update.get("message")
        if type(update_id) is not int or update_id < 0 or not isinstance(message, dict):
            return None
        chat, sender = message.get("chat"), message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        chat_id, sender_id = chat.get("id"), sender.get("id")
        if type(chat_id) is not int or type(sender_id) is not int:
            return None
        binding = self._bindings.get(chat_id)
        if (
            binding is None
            or sender_id not in binding.allowed_user_ids
            or sender.get("is_bot") is not False
            or message.get("sender_chat") is not None
            or chat.get("type") not in {"private", "group", "supergroup"}
        ):
            return None
        text, date = message.get("text"), message.get("date")
        if not isinstance(text, str) or not text.strip() or type(date) is not int:
            return None
        try:
            occurred_at = datetime.fromtimestamp(date, UTC)
            text.encode("utf-8")
        except (ValueError, OverflowError, OSError, UnicodeError):
            return None
        endpoint = self.gateway.conversation.connect(
            binding.terminal_id, binding.conversation_id, source_id="telegram"
        )
        return await endpoint.send(
            text, producer_event_id=f"telegram:{update_id}", occurred_at=occurred_at
        )

    async def poll_once(self) -> int:
        """取得一批更新，按顺序持久化；异常时保留尚未成功更新的游标。"""
        self._register_bindings()
        async with self._poll_lock:
            updates = await self._request(
                "getUpdates",
                {
                    "offset": self._offset,
                    "timeout": self.settings.poll_timeout,
                    "allowed_updates": ["message"],
                    "limit": 100,
                },
            )
            if not isinstance(updates, list):
                raise TelegramAPIError()
            if any(
                not isinstance(update, dict)
                or type(update.get("update_id")) is not int
                or update["update_id"] < 0
                for update in updates
            ):
                raise TelegramAPIError()
            received = 0
            for update in sorted(updates, key=lambda item: item["update_id"]):
                if update["update_id"] < self._offset:
                    continue
                receipt = await self.process_update(update)
                self._offset = update["update_id"] + 1
                if receipt is not None and not receipt.duplicate:
                    received += 1
            return received

    async def _poll(self) -> None:
        try:
            # 支持立即执行任务工厂，等待 start 记录好任务句柄。
            await asyncio.sleep(0)
            while not self._closed:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    retry_after = exc.retry_after if isinstance(exc, TelegramAPIError) else 0.0
                    logger.warning(
                        "Telegram 接收失败，稍后重试",
                        extra={
                            "error_type": type(exc).__name__,
                            "status": exc.status if isinstance(exc, TelegramAPIError) else None,
                        },
                    )
                    await asyncio.sleep(max(self.settings.retry_delay, retry_after))
                else:
                    # 即使服务器立即返回空列表，也避免断线重连时持续空转。
                    await asyncio.sleep(0.1)
        finally:
            logger.info("Telegram 接收循环已结束")

    async def _send_output(self, binding: TelegramBindingSettings, output: OutputRecord) -> None:
        self._ensure_open()
        if (output.terminal_id, output.user_id, output.conversation_id) != (
            binding.terminal_id,
            binding.user_id,
            binding.conversation_id,
        ):
            raise ValueError("Output does not belong to the Telegram binding")
        chunks = _text_chunks(output.text)
        digest = hashlib.sha256(output.text.encode("utf-8")).hexdigest()
        previous = self._progress.get(output.output_id)
        if previous is not None and previous[0] != digest:
            raise ValueError("Output changed while Telegram delivery was in progress")
        index = previous[1] if previous is not None else 0
        for chunk in chunks[index:]:
            await self._request("sendMessage", {"chat_id": binding.chat_id, "text": chunk})
            index += 1
            self._progress[output.output_id] = (digest, index)
            self._progress.move_to_end(output.output_id)
            while len(self._progress) > 256:
                self._progress.popitem(last=False)
        logger.info(
            "Telegram 回复已发送",
            extra={"output_id": output.output_id, "terminal_id": binding.terminal_id},
        )

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(), name="wintermute-telegram-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        for disconnect in self._disconnectors:
            disconnect()
        self._disconnectors.clear()
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._progress.clear()


__all__ = ["TelegramAPIError", "TelegramInterface"]
