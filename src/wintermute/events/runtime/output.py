"""持久化输出的终端路由；未连接的终端保留待投递记录。"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy

from wintermute.events.core.types import OutputRecord
from wintermute.events.storage.sqlite import SQLiteStore

type OutputHandler = Callable[[OutputRecord], Awaitable[None] | None]


class OutputRouter:
    """处理状态与投递状态分开，按 output_id 支持终端幂等确认。

    handler 成功返回表示该终端已确认接收。发送后、确认前断线会导致
    再次投递同一 output_id，因此终端应据此去重。无目标的输出供收件箱读取。
    """

    def __init__(self, store: SQLiteStore, *, delivery_timeout: float = 30.0) -> None:
        if delivery_timeout <= 0 or not delivery_timeout < float("inf"):
            raise ValueError("delivery_timeout must be finite and positive")
        self.store = store
        self.delivery_timeout = delivery_timeout
        self._handlers: dict[str, OutputHandler] = {}
        self._lock = asyncio.Lock()
        self._delivering: ContextVar[bool] = ContextVar(
            f"wintermute-output-{id(self)}", default=False
        )

    def connect(self, terminal_id: str, handler: OutputHandler) -> Callable[[], None]:
        self.store.get_terminal(terminal_id)
        if not callable(handler):
            raise TypeError("Output handler must be callable")
        self._handlers[terminal_id] = handler

        def disconnect() -> None:
            if self._handlers.get(terminal_id) is handler:
                del self._handlers[terminal_id]

        return disconnect

    async def deliver_pending(self) -> int:
        """每条已连接终端的待投递输出尝试一次；失败留存供下次调用。"""
        if self._delivering.get():
            raise RuntimeError("Output handlers cannot wait on their own router")
        token = self._delivering.set(True)
        try:
            return await self._deliver_pending()
        finally:
            self._delivering.reset(token)

    async def _deliver_pending(self) -> int:
        delivered = 0
        async with self._lock:
            pending = tuple(
                output
                for terminal_id in tuple(self._handlers)
                for output in self.store.pending_outputs(terminal_id=terminal_id)
            )
            for output in pending:
                handler = self._handlers.get(output.terminal_id or "")
                if handler is None:
                    continue
                try:
                    async with asyncio.timeout(self.delivery_timeout):
                        result = handler(deepcopy(output))
                        if inspect.isawaitable(result):
                            await result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.mark_output_failed(output.output_id, f"{type(exc).__name__}: {exc}")
                else:
                    self.store.mark_output_delivered(output.output_id)
                    delivered += 1
        return delivered
