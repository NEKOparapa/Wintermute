"""持久化输出的设备路由；未连接的设备保留待投递记录。"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy

from wintermute.events.core.types import OutputRecord
from wintermute.events.storage.sqlite import SQLiteStore

type OutputHandler = Callable[[OutputRecord], Awaitable[None] | None]


class OutputRouter:
    """处理状态与投递状态分开，按 output_id 支持设备幂等确认。

    handler 成功返回表示该设备已确认接收。发送后、确认前断线会导致
    再次投递同一 output_id，因此设备应据此去重。无目标的输出供收件箱读取。
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

    def connect(self, device_id: str, handler: OutputHandler) -> Callable[[], None]:
        self.store.get_device(device_id)
        if not callable(handler):
            raise TypeError("Output handler must be callable")
        self._handlers[device_id] = handler

        def disconnect() -> None:
            if self._handlers.get(device_id) is handler:
                del self._handlers[device_id]

        return disconnect

    async def deliver_pending(self) -> int:
        """每条已连接设备的待投递输出尝试一次；失败留存供下次调用。"""
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
                for device_id in tuple(self._handlers)
                for output in self.store.outputs.pending_outputs(device_id=device_id)
            )
            for output in pending:
                handler = self._handlers.get(output.device_id or "")
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
                    self.store.outputs.mark_output_failed(
                        output.output_id, f"{type(exc).__name__}: {exc}"
                    )
                else:
                    self.store.outputs.mark_output_delivered(output.output_id)
                    delivered += 1
        return delivered
