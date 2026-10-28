"""把待处理的原始切片分批压缩并原子加入每日切片流。"""

import asyncio
import logging
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.types import EventRecord, SchedulerBusyError
from wintermute.events.slices.compression import (
    RuleSliceCompressor,
    SliceCompressionError,
    SliceCompressor,
    bounded_text,
    serialize_slices,
    validate_output_limit,
)
from wintermute.events.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class SliceProcessor:
    """独立于交流和感知状态的切片处理器，调用者负责后台任务生命周期。

    每次读取有限数量的待处理事件，按用户、会话范围、终端、来源和当地
    自然日分组。只压缩新切片，不重复改写整日摘要；原始事件始终保留。
    失败延迟重试，取消不消耗事件，重启后继续处理尚未提交的记录。
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        timezone: str = "Asia/Shanghai",
        batch_size: int = 32,
        max_input_chars: int = 12_000,
        max_output_chars: int = 1_200,
        retry_delay: float = 5.0,
        compressor: SliceCompressor | None = None,
    ) -> None:
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 10000
        ):
            raise ValueError("batch_size must be an integer between 1 and 10000")
        if (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars < 512
        ):
            raise ValueError("max_input_chars must be an integer >= 512")
        validate_output_limit(max_output_chars)
        if isinstance(retry_delay, bool) or not math.isfinite(retry_delay) or retry_delay <= 0:
            raise ValueError("retry_delay must be finite and positive")
        self.store = store
        self.timezone = timezone
        self._zone = ZoneInfo(timezone)
        self.batch_size = batch_size
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self.retry_delay = retry_delay
        self.compressor = compressor if compressor is not None else RuleSliceCompressor()
        self._lock = asyncio.Lock()

    def _normalized(self, event: EventRecord) -> EventRecord:
        text = " ".join(str(event.payload["text"]).split())
        return event.model_copy(update={"payload": {"text": text}}, deep=True)

    def _fit_single(self, event: EventRecord) -> EventRecord:
        if len(serialize_slices((event,), timezone=self._zone)) <= self.max_input_chars:
            return event
        text = str(event.payload["text"])
        low, high = 0, len(text)
        # JSON 转义也占用输入额度，二分查找真正可序列化的文本上限。
        while low < high:
            middle = (low + high + 1) // 2
            candidate = event.model_copy(update={"payload": {"text": bounded_text(text, middle)}})
            if len(serialize_slices((candidate,), timezone=self._zone)) <= self.max_input_chars:
                low = middle
            else:
                high = middle - 1
        result = event.model_copy(update={"payload": {"text": bounded_text(text, low)}})
        if not low or len(serialize_slices((result,), timezone=self._zone)) > self.max_input_chars:
            raise SliceCompressionError("Slice metadata exceeds max_input_chars")
        return result

    def _chunks(self, events: list[EventRecord]) -> Iterator[tuple[EventRecord, ...]]:
        current: list[EventRecord] = []
        for event in sorted(
            events, key=lambda item: (item.occurred_at, item.received_at, item.event_id)
        ):
            if (
                current
                and len(serialize_slices((*current, event), timezone=self._zone))
                > self.max_input_chars
            ):
                yield tuple(current)
                current = []
            current.append(event)
        if current:
            yield tuple(current)

    def _record_failure(
        self, event_ids: tuple[str, ...], exc: Exception, owner_id: str | None
    ) -> None:
        self.store.mark_slice_failed(
            event_ids,
            type(exc).__name__,
            retry_delay=self.retry_delay,
            owner_id=owner_id,
        )
        logger.warning(
            "切片压缩失败，原始记录等待重试",
            extra={"error_type": type(exc).__name__, "slice_count": len(event_ids)},
        )

    async def process_pending(self, *, owner_id: str | None = None) -> int:
        """完成至多 batch_size 条记录，返回本次成功持久化的原事件数量。"""
        async with self._lock:
            events = self.store.pending_slices(limit=self.batch_size)
            as_of = datetime.now(UTC)
            groups: dict[tuple[str, str | None, str, str, str, bool], list[EventRecord]] = {}
            for event in events:
                try:
                    prepared = self._fit_single(self._normalized(event))
                except Exception as exc:
                    self._record_failure((event.event_id,), exc, owner_id)
                    continue
                day = event.occurred_at.astimezone(self._zone).date().isoformat()
                # 将未来时间的上游记录分开，避免其范围遮住已经发生的切片。
                key = (
                    event.user_id,
                    event.conversation_id,
                    event.terminal_id,
                    event.source_id,
                    day,
                    event.occurred_at > as_of,
                )
                groups.setdefault(key, []).append(prepared)
            processed = 0
            for key, group in groups.items():
                for batch in self._chunks(group):
                    # 同步策略也应允许主会话和取消请求取得执行机会。
                    await asyncio.sleep(0)
                    event_ids = tuple(event.event_id for event in batch)
                    try:
                        result = await self.compressor.compress(
                            batch, max_chars=self.max_output_chars
                        )
                        if not isinstance(result, str) or not result.strip():
                            raise SliceCompressionError("Slice compressor returned empty text")
                        text = bounded_text(result.strip(), self.max_output_chars)
                        item = SliceStreamItem(
                            item_id=f"slice_{uuid4().hex}",
                            user_id=key[0],
                            conversation_id=key[1],
                            terminal_id=key[2],
                            source_id=key[3],
                            day=key[4],
                            timezone=self.timezone,
                            started_at=batch[0].occurred_at,
                            ended_at=batch[-1].occurred_at,
                            source_event_ids=event_ids,
                            text=text,
                            strategy=self.compressor.strategy,
                            created_at=datetime.now(UTC),
                        )
                        self.store.commit_slice(item, owner_id=owner_id)
                    except SchedulerBusyError:
                        # 租约失效必须交由调度器处理，不能伪装成普通压缩失败。
                        raise
                    except Exception as exc:
                        self._record_failure(event_ids, exc, owner_id)
                    else:
                        processed += len(batch)
                        logger.info(
                            "切片已加入每日切片流",
                            extra={
                                "user_id": key[0],
                                "conversation_id": key[1],
                                "terminal_id": key[2],
                                "slice_count": len(batch),
                                "slice_strategy": item.strategy,
                            },
                        )
            return processed
