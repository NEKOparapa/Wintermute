"""四个终端入口共用的事件校验、持久化与唤醒接口。"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from wintermute.events.core.types import EventInput, Layer, Receipt, Terminal
from wintermute.events.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class EventGateway:
    """先提交数据库事务，再通知调度器；不执行模型或音视频处理。

    本接口供可信应用代码调用。HTTP、WebSocket 等适配器须先认证连接，
    再绑定注册终端，不能将请求中自报的 terminal_id 当作登录凭据。
    """

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self._listeners: list[Callable[[], None]] = []
        self.conversation = ConversationIngress(self)
        self.notification = NotificationIngress(self)
        self.perception = PerceptionIngress(self)
        self.slice = SliceIngress(self)

    def register_terminal(self, terminal: Terminal) -> None:
        self.store.register_terminal(terminal)

    def register_conversation(self, conversation_id: str, user_id: str) -> None:
        self.store.register_conversation(conversation_id, user_id)

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册轻量同步唤醒回调；失败不影响已经提交的接收回执。"""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    async def submit(self, event: EventInput) -> Receipt:
        """返回已持久化回执；发送端重试须复用 producer_event_id。"""
        receipt = self.store.append(event)
        logger.info(
            "事件已接收",
            extra={
                "event_id": receipt.event.event_id,
                "conversation_id": event.conversation_id,
                "terminal_id": event.terminal_id,
                "layer": event.layer,
                "job_id": receipt.job_id,
                "duplicate": receipt.duplicate,
            },
        )
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception as exc:
                logger.error(
                    "事件已保存，但唤醒回调失败",
                    extra={"event_id": receipt.event.event_id, "error_type": type(exc).__name__},
                )
        return receipt


class TerminalEndpoint:
    """将一个已注册终端绑定到特定入口和可选的目标会话。"""

    def __init__(
        self,
        gateway: EventGateway,
        terminal_id: str,
        layer: Layer,
        *,
        conversation_id: str | None,
        source_id: str = "default",
        reply_terminal_id: str | None = None,
    ) -> None:
        terminal = gateway.store.get_terminal(terminal_id)
        if layer not in terminal.allowed_layers:
            raise ValueError("Terminal is not allowed to use this layer")
        if conversation_id is not None:
            if gateway.store.get_conversation_user(conversation_id) != terminal.user_id:
                raise ValueError("Conversation belongs to another user")
        if reply_terminal_id is not None:
            if gateway.store.get_terminal(reply_terminal_id).user_id != terminal.user_id:
                raise ValueError("Reply terminal belongs to another user")
        self.gateway = gateway
        self.terminal_id = terminal_id
        self.layer = layer
        self.conversation_id = conversation_id
        self.source_id = source_id
        self.reply_terminal_id = reply_terminal_id

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        producer_event_id: str | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
        source_seq: int | None = None,
    ) -> Receipt:
        return await self.gateway.submit(
            EventInput(
                producer_event_id=uuid4().hex if producer_event_id is None else producer_event_id,
                terminal_id=self.terminal_id,
                layer=self.layer,
                type=event_type,
                source_id=self.source_id,
                conversation_id=self.conversation_id,
                reply_terminal_id=self.reply_terminal_id,
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC) if occurred_at is None else occurred_at,
                source_seq=source_seq,
                payload=payload,
            )
        )


class ConversationEndpoint(TerminalEndpoint):
    """用户消息入口；回复默认返回发送终端。"""

    async def send(
        self,
        text: str,
        *,
        producer_event_id: str | None = None,
        occurred_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> Receipt:
        return await self.publish(
            "user.message",
            {"text": text},
            producer_event_id=producer_event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
        )


class PerceptionEndpoint(TerminalEndpoint):
    """接收上游处理好的状态或活动事件，不处理原始媒体流。"""

    async def observe(
        self,
        state_key: str,
        value: Any,
        *,
        subject: str | None = None,
        valid_until: datetime | None = None,
        producer_event_id: str | None = None,
        occurred_at: datetime | None = None,
        source_seq: int | None = None,
    ) -> Receipt:
        payload = {
            "subject": f"device:{self.terminal_id}" if subject is None else subject,
            "state_key": state_key,
            "value": value,
        }
        if valid_until is not None:
            payload["valid_until"] = valid_until.isoformat()
        return await self.publish(
            "state.observed",
            payload,
            producer_event_id=producer_event_id,
            occurred_at=occurred_at,
            source_seq=source_seq,
        )


class ConversationIngress:
    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self, terminal_id: str, conversation_id: str, *, source_id: str = "default"
    ) -> ConversationEndpoint:
        return ConversationEndpoint(
            self.gateway,
            terminal_id,
            "conversation",
            conversation_id=conversation_id,
            source_id=source_id,
            reply_terminal_id=terminal_id,
        )


class SliceEndpoint(TerminalEndpoint):
    """接收文字切片或已经落盘的音频引用，原始资料在压缩前保留。"""

    async def record_audio(
        self,
        audio: dict[str, Any],
        *,
        producer_event_id: str | None = None,
        occurred_at: datetime | None = None,
        source_seq: int | None = None,
    ) -> Receipt:
        return await self.publish(
            "slice.audio",
            {"audio": audio},
            producer_event_id=producer_event_id,
            occurred_at=occurred_at,
            source_seq=source_seq,
        )

    async def record(
        self,
        text: str,
        *,
        producer_event_id: str | None = None,
        occurred_at: datetime | None = None,
        source_seq: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Receipt:
        payload: dict[str, Any] = {"text": text}
        if metadata is not None:
            payload["metadata"] = metadata
        return await self.publish(
            "slice.recorded",
            payload,
            producer_event_id=producer_event_id,
            occurred_at=occurred_at,
            source_seq=source_seq,
        )


class SliceIngress:
    """将多个终端和来源接入切片层，不产生主 Agent 执行任务。"""

    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self,
        terminal_id: str,
        *,
        conversation_id: str | None = None,
        source_id: str = "default",
    ) -> SliceEndpoint:
        return SliceEndpoint(
            self.gateway,
            terminal_id,
            "slice",
            conversation_id=conversation_id,
            source_id=source_id,
        )


class NotificationIngress:
    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self,
        terminal_id: str,
        conversation_id: str,
        *,
        source_id: str = "default",
        reply_terminal_id: str | None = None,
    ) -> TerminalEndpoint:
        return TerminalEndpoint(
            self.gateway,
            terminal_id,
            "notification",
            conversation_id=conversation_id,
            source_id=source_id,
            reply_terminal_id=reply_terminal_id,
        )


class PerceptionIngress:
    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self,
        terminal_id: str,
        *,
        conversation_id: str | None = None,
        source_id: str = "default",
    ) -> PerceptionEndpoint:
        return PerceptionEndpoint(
            self.gateway,
            terminal_id,
            "perception",
            conversation_id=conversation_id,
            source_id=source_id,
        )
