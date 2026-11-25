"""设备事件与历史记录共用的校验、持久化与唤醒接口。"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import JsonValue

from purin.events.core.types import Device, EventInput, Layer, Receipt
from purin.events.storage.repositories.records import RecordBatch, RecordBatchInput
from purin.events.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class EventGateway:
    """先提交数据库事务，再通知调度器；不执行模型或音视频处理。

    本接口供可信应用代码调用。HTTP、WebSocket 等适配器须先认证连接，
    再绑定注册设备，不能将请求中自报的 device_id 当作登录凭据。
    """

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self._listeners: list[Callable[[], None]] = []
        self.conversation = ConversationIngress(self)
        self.notification = NotificationIngress(self)
        self.perception = PerceptionIngress(self)
        self.slice = SliceIngress(self)
        self.record = RecordIngress(self)

    def register_device(self, device: Device) -> None:
        self.store.register_device(device)

    def register_conversation(self, conversation_id: str, user_id: str) -> None:
        self.store.register_conversation(conversation_id, user_id)

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册轻量同步唤醒回调；失败不影响已经提交的接收回执。"""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    async def submit(self, event: EventInput, *, images: tuple[bytes, ...] = ()) -> Receipt:
        """返回已持久化回执；发送端重试须复用 producer_event_id。"""
        receipt = self.store.append(event, images=images)
        logger.info(
            "事件已接收",
            extra={
                "event_id": receipt.event.event_id,
                "conversation_id": event.conversation_id,
                "device_id": event.device_id,
                "layer": event.layer,
                "job_id": receipt.job_id,
                "duplicate": receipt.duplicate,
            },
        )
        self.wakeup()
        return receipt

    def wakeup(self) -> None:
        """Wake consumers after a committed event or historical record batch."""
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception as exc:
                logger.error(
                    "事件已保存，但唤醒回调失败",
                    extra={"error_type": type(exc).__name__},
                )

    async def submit_records(self, device_id: str, batch: RecordBatchInput) -> RecordBatch:
        result = self.store.records.submit(device_id, batch)
        self.wakeup()
        return result


class RecordEndpoint:
    """A historical import endpoint bound to a registered device, never a main session."""

    def __init__(self, gateway: EventGateway, device_id: str) -> None:
        if "record" not in gateway.store.get_device(device_id).allowed_layers:
            raise ValueError("Device is not allowed to import records")
        self.gateway = gateway
        self.device_id = device_id

    async def submit(self, batch: RecordBatchInput) -> RecordBatch:
        return await self.gateway.submit_records(self.device_id, batch)


class RecordIngress:
    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(self, device_id: str) -> RecordEndpoint:
        return RecordEndpoint(self.gateway, device_id)


class DeviceEndpoint:
    """将一个已注册设备绑定到特定入口和可选的目标会话。"""

    def __init__(
        self,
        gateway: EventGateway,
        device_id: str,
        layer: Layer,
        *,
        conversation_id: str | None,
        source_id: str = "default",
        reply_device_id: str | None = None,
    ) -> None:
        device = gateway.store.get_device(device_id)
        if layer not in device.allowed_layers:
            raise ValueError("Device is not allowed to use this layer")
        if conversation_id is not None:
            if gateway.store.get_conversation_user(conversation_id) != device.user_id:
                raise ValueError("Conversation belongs to another user")
        if reply_device_id is not None:
            if gateway.store.get_device(reply_device_id).user_id != device.user_id:
                raise ValueError("Reply device belongs to another user")
        self.gateway = gateway
        self.device_id = device_id
        self.layer = layer
        self.conversation_id = conversation_id
        self.source_id = source_id
        self.reply_device_id = reply_device_id

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        producer_event_id: str | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
        source_seq: int | None = None,
        images: tuple[bytes, ...] = (),
    ) -> Receipt:
        return await self.gateway.submit(
            EventInput(
                producer_event_id=uuid4().hex if producer_event_id is None else producer_event_id,
                device_id=self.device_id,
                layer=self.layer,
                type=event_type,
                source_id=self.source_id,
                conversation_id=self.conversation_id,
                reply_device_id=self.reply_device_id,
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC) if occurred_at is None else occurred_at,
                source_seq=source_seq,
                payload=payload,
            ),
            images=images,
        )


class ConversationEndpoint(DeviceEndpoint):
    """用户消息入口；回复默认返回发送设备。"""

    async def send(
        self,
        text: str,
        *,
        producer_event_id: str | None = None,
        occurred_at: datetime | None = None,
        correlation_id: str | None = None,
        images: tuple[bytes, ...] = (),
        image_names: list[str] | None = None,
        documents: list[dict[str, JsonValue]] | None = None,
    ) -> Receipt:
        return await self.publish(
            "user.message",
            {
                "text": text,
                **({"documents": documents} if documents else {}),
                **({"image_names": image_names} if image_names else {}),
            },
            producer_event_id=producer_event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            images=images,
        )


class PerceptionEndpoint(DeviceEndpoint):
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
            "subject": f"device:{self.device_id}" if subject is None else subject,
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
        self, device_id: str, conversation_id: str, *, source_id: str = "default"
    ) -> ConversationEndpoint:
        return ConversationEndpoint(
            self.gateway,
            device_id,
            "conversation",
            conversation_id=conversation_id,
            source_id=source_id,
            reply_device_id=device_id,
        )


class SliceEndpoint(DeviceEndpoint):
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
    """将多个设备和来源接入切片层，不产生主 Agent 执行任务。"""

    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self,
        device_id: str,
        *,
        conversation_id: str | None = None,
        source_id: str = "default",
    ) -> SliceEndpoint:
        return SliceEndpoint(
            self.gateway,
            device_id,
            "slice",
            conversation_id=conversation_id,
            source_id=source_id,
        )


class NotificationIngress:
    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self,
        device_id: str,
        conversation_id: str,
        *,
        source_id: str = "default",
        reply_device_id: str | None = None,
    ) -> DeviceEndpoint:
        return DeviceEndpoint(
            self.gateway,
            device_id,
            "notification",
            conversation_id=conversation_id,
            source_id=source_id,
            reply_device_id=reply_device_id,
        )


class PerceptionIngress:
    def __init__(self, gateway: EventGateway) -> None:
        self.gateway = gateway

    def connect(
        self,
        device_id: str,
        *,
        conversation_id: str | None = None,
        source_id: str = "default",
    ) -> PerceptionEndpoint:
        return PerceptionEndpoint(
            self.gateway,
            device_id,
            "perception",
            conversation_id=conversation_id,
            source_id=source_id,
        )
