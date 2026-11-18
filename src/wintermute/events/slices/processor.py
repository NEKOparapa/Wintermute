"""按采集时间保存每条切片及其识别结果。"""

import asyncio
import logging
import math
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.types import SchedulerBusyError
from wintermute.events.slices.audio import AudioSliceProcessor
from wintermute.events.slices.compression import (
    SliceCompressionError,
    SliceCompressor,
    validate_output_limit,
)
from wintermute.events.slices.image import ImageSliceProcessor, format_capture_facts
from wintermute.events.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class SliceProcessor:
    """独立于交流和感知状态的切片处理器，调用者负责后台任务生命周期。

    文本保留原文，音频保留转写，图片保留识别结果。跨事件摘要由合并线生成。
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
        audio_processor: AudioSliceProcessor | None = None,
        image_processor: ImageSliceProcessor | None = None,
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
        self.audio_processor = audio_processor
        self.image_processor = image_processor
        self._lock = asyncio.Lock()

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
            "切片处理失败，原始记录等待重试",
            extra={"error_type": type(exc).__name__, "slice_count": len(event_ids)},
        )

    async def process_pending(self, *, owner_id: str | None = None) -> int:
        """完成至多 batch_size 条记录，返回本次成功持久化的原事件数量。"""
        async with self._lock:
            events = self.store.pending_slices(limit=self.batch_size)
            processed = 0
            for event in events:
                await asyncio.sleep(0)
                try:
                    if event.type == "slice.image" and self.image_processor is not None:
                        day = event.occurred_at.astimezone(self._zone).date().isoformat()
                        previous = self.store.preceding_screenshot(event, day)
                        observation = await self.image_processor.observe(
                            event, previous.reference if previous else None
                        )
                        self.store.commit_screenshot(
                            event,
                            previous,
                            observation,
                            timezone=self.timezone,
                            text=format_capture_facts(
                                event, observation, max_chars=self.max_output_chars
                            ),
                            owner_id=owner_id,
                        )
                        processed += 1
                        continue
                    elif event.type == "slice.audio" and self.audio_processor is not None:
                        result = await self.audio_processor.process(event, owner_id=owner_id)
                        strategy = self.audio_processor.strategy
                    elif event.type == "slice.recorded":
                        result, strategy = str(event.payload["text"]), "original"
                    else:
                        raise SliceCompressionError(f"{event.type} processing is disabled")
                    item = SliceStreamItem(
                        item_id=f"slice_{uuid4().hex}",
                        user_id=event.user_id,
                        conversation_id=event.conversation_id,
                        terminal_id=event.terminal_id,
                        source_id=event.source_id,
                        day=event.occurred_at.astimezone(self._zone).date().isoformat(),
                        timezone=self.timezone,
                        started_at=event.occurred_at,
                        ended_at=event.occurred_at,
                        source_event_ids=(event.event_id,),
                        text=result,
                        strategy=strategy,
                        created_at=datetime.now(UTC),
                    )
                    self.store.commit_slice(item, owner_id=owner_id)
                except SchedulerBusyError:
                    raise
                except Exception as exc:
                    self._record_failure((event.event_id,), exc, owner_id)
                else:
                    processed += 1
            return processed
