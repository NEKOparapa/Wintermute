"""切片原文、音频转写和截图分组持久化。"""

import math
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from wintermute.events.core.activity import (
    slice_activity,
)
from wintermute.events.core.slices import SliceProcessingStatus, SliceStreamItem
from wintermute.events.core.types import (
    EventError,
    EventRecord,
)
from wintermute.events.storage import screenshot_groups

if TYPE_CHECKING:
    from wintermute.events.slices.image import ScreenshotObservation
    from wintermute.events.storage.sqlite import SQLiteStore


class SliceStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def pending_slices(self, *, limit: int = 100) -> tuple[EventRecord, ...]:
        """读取可处理原始切片；失败项到达重试时间后重新成为候选。"""
        self._store._ensure_open()
        rows = self._store._connection.execute(
            "SELECT e.data FROM slice_processing p INDEXED BY slice_waiting "
            "JOIN slice_events e ON p.event_id=e.event_id "
            "WHERE p.item_id IS NULL AND p.frame_done=0 AND (p.retry_at IS NULL OR p.retry_at<=?) "
            "ORDER BY e.seq LIMIT ?",
            (self._store._now().timestamp(), self._store._limit(limit)),
        ).fetchall()
        return tuple(EventRecord.model_validate_json(row["data"]) for row in rows)

    def has_unprocessed_slices(self) -> bool:
        """包含仍在等待重试的事件，避免将暂时无候选误判为处理完成。"""
        self._store._ensure_open()
        return (
            self._store._connection.execute(
                "SELECT 1 FROM slice_processing INDEXED BY slice_waiting "
                "WHERE item_id IS NULL AND frame_done=0 LIMIT 1"
            ).fetchone()
            is not None
        )

    def commit_slice(self, item: SliceStreamItem, *, owner_id: str | None = None) -> None:
        """原子保存摘要及来源；同一组来源的修订保留旧版及其历史。"""
        item = SliceStreamItem.model_validate(item.model_dump())
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            self._store._check_scope(item.conversation_id, item.user_id)
            existing = self._store._connection.execute(
                "SELECT data FROM slice_stream WHERE item_id=?", (item.item_id,)
            ).fetchone()
            if existing is not None:
                if SliceStreamItem.model_validate_json(existing["data"]) == item:
                    return
                raise EventError("Slice stream item ID was reused with different content")
            source_events: list[EventRecord] = []
            previous_ids: set[str | None] = set()
            for event_id in item.source_event_ids:
                event = self._store.get_event(event_id, user_id=item.user_id)
                if (
                    event.layer != "slice"
                    or event.conversation_id != item.conversation_id
                    or event.terminal_id != item.terminal_id
                    or event.source_id != item.source_id
                ):
                    raise EventError("Slice stream sources must have matching ownership and scope")
                state = self._store._connection.execute(
                    "SELECT item_id FROM slice_processing WHERE event_id=?", (event_id,)
                ).fetchone()
                if state is None:
                    raise EventError("Slice source is missing")
                previous_ids.add(state["item_id"])
                source_events.append(event)
            if len(previous_ids) != 1:
                raise EventError("Slice sources overlap previously processed groups")
            previous = previous_ids.pop()
            if previous is not None:
                stored = self._store._connection.execute(
                    "SELECT data FROM slice_stream WHERE item_id=?", (previous,)
                ).fetchone()
                assert stored is not None
                original = SliceStreamItem.model_validate_json(stored["data"])
                if set(original.source_event_ids) != set(item.source_event_ids):
                    raise EventError("Slice revision must include all previously processed sources")
            times = [event.occurred_at for event in source_events]
            if min(times) != item.started_at or max(times) != item.ended_at:
                raise EventError("Slice stream range must match its source event timestamps")
            self._write_slice(item, source_events, previous)
            if (
                len(source_events) == 1
                and source_events[0].type == "slice.image"
                and "capture" in source_events[0].payload
            ):
                screenshot_groups.record_existing_item(
                    self._store._connection, source_events[0], item
                )

    def _write_slice(
        self,
        item: SliceStreamItem,
        source_events: list[EventRecord],
        previous: str | None = None,
    ) -> None:
        self._store._connection.execute(
            "INSERT INTO slice_stream(item_id,user_id,conversation_scope,day,timezone,"
            "started_at,ended_at,created_at,data) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item.item_id,
                item.user_id,
                item.conversation_id or "",
                item.day,
                item.timezone,
                item.started_at.timestamp(),
                item.ended_at.timestamp(),
                item.created_at.timestamp(),
                item.model_dump_json(),
            ),
        )
        self._store._connection.executemany(
            "UPDATE slice_processing SET item_id=?,attempts=attempts+1,"
            "error_type=NULL,retry_at=NULL WHERE event_id=?",
            [(item.item_id, event_id) for event_id in item.source_event_ids],
        )
        known_at = self._store._now()
        if previous is not None:
            self._store._connection.execute(
                "INSERT INTO slice_revisions(previous_id,replacement_id,known_at) VALUES(?,?,?)",
                (previous, item.item_id, known_at.timestamp()),
            )
        self._store.timeline._project_activity(
            slice_activity(item, source_events, known_at=known_at),
        )

    def preceding_screenshot(
        self, event: EventRecord, day: str
    ) -> screenshot_groups.PreviousScreenshot | None:
        self._store._ensure_open()
        return screenshot_groups.previous_capture(self._store._connection, event, day)

    def commit_screenshot(
        self,
        event: EventRecord,
        previous: screenshot_groups.PreviousScreenshot | None,
        observation: "ScreenshotObservation",
        *,
        timezone: str,
        text: str,
        owner_id: str | None = None,
    ) -> None:
        """Commit a processed capture, including unchanged frames that have no timeline item."""
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            source = self._store.get_event(event.event_id, user_id=event.user_id)
            if source != event or event.type != "slice.image" or "capture" not in event.payload:
                raise EventError("Expected a stored periodic screenshot")
            status = self.get_slice_status(event.event_id)
            if status.status == "completed":
                return
            day = event.occurred_at.astimezone(ZoneInfo(timezone)).date().isoformat()
            # A lease change or second processor may have committed a preceding frame during
            # inference. Retry against the new facts instead of storing a stale comparison.
            if previous != self.preceding_screenshot(event, day):
                raise EventError("Screenshot reference changed during recognition")
            item_id: str | None = None
            if observation.facts:
                item = SliceStreamItem(
                    item_id=f"slice_{uuid4().hex}",
                    user_id=event.user_id,
                    conversation_id=event.conversation_id,
                    terminal_id=event.terminal_id,
                    source_id=event.source_id,
                    day=day,
                    timezone=timezone,
                    started_at=event.occurred_at,
                    ended_at=event.occurred_at,
                    source_event_ids=(event.event_id,),
                    text=text,
                    strategy="image_description",
                    created_at=self._store._now(),
                )
                self._write_slice(item, [event])
                item_id = item.item_id
            else:
                self._store._connection.execute(
                    "UPDATE slice_processing SET frame_done=1,attempts=attempts+1,"
                    "error_type=NULL,retry_at=NULL WHERE event_id=?",
                    (event.event_id,),
                )
            screenshot_groups.save_capture(
                self._store._connection, event, day, previous, observation, item_id
            )

    def get_audio_transcript(self, event_id: str, *, user_id: str | None = None) -> str | None:
        """返回完整转录；None 表示未转录，空字符串表示已确认静音。"""
        if self._store.get_event(event_id, user_id=user_id).type != "slice.audio":
            raise EventError("Only audio slices have transcripts")
        row = self._store._connection.execute(
            "SELECT text FROM audio_transcripts WHERE event_id=?", (event_id,)
        ).fetchone()
        return str(row["text"]) if row is not None else None

    def save_audio_transcript(
        self, event_id: str, text: str, *, owner_id: str | None = None
    ) -> None:
        """ASR 成功后独立提交，使总结失败和服务重启不会重新付费转录。"""
        if not isinstance(text, str):
            raise ValueError("Transcript must be text")
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            existing = self.get_audio_transcript(event_id)
            if existing is not None:
                if existing != text:
                    raise EventError("Audio transcript is immutable")
                return
            self._store._connection.execute(
                "INSERT INTO audio_transcripts(event_id,text,created_at,user_id,conversation_id) "
                "SELECT event_id,?,?,user_id,conversation_id FROM slice_events WHERE event_id=?",
                (text, self._store._now().timestamp(), event_id),
            )

    def mark_slice_failed(
        self,
        event_ids: Sequence[str],
        error_type: str,
        *,
        retry_delay: float,
        owner_id: str | None = None,
    ) -> None:
        """保存失败次数和重试时间；取消操作应保留待处理状态而不调用本方法。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", error_type):
            raise ValueError("error_type must contain only an exception type name")
        if isinstance(retry_delay, bool) or not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("retry_delay must be finite and nonnegative")
        if not event_ids or len(set(event_ids)) != len(event_ids):
            raise ValueError("event_ids must be nonempty and unique")
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            for event_id in event_ids:
                if self._store.get_event(event_id).layer != "slice":
                    raise EventError("Only slice events can record processing failure")
            retry_at = self._store._now().timestamp() + retry_delay
            self._store._connection.executemany(
                "UPDATE slice_processing SET attempts=attempts+1,error_type=?,retry_at=? "
                "WHERE event_id=? AND item_id IS NULL AND frame_done=0",
                [(error_type, retry_at, event_id) for event_id in event_ids],
            )

    def get_slice_status(
        self, event_id: str, *, user_id: str | None = None
    ) -> SliceProcessingStatus:
        """按事件查询处理结果；可通过用户归属限制调用方的查询范围。"""
        if self._store.get_event(event_id, user_id=user_id).layer != "slice":
            raise EventError("Only slice events have slice processing status")
        row = self._store._connection.execute(
            "SELECT * FROM slice_processing WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise EventError("Slice event has no processing status")
        retry_at = row["retry_at"]
        return SliceProcessingStatus(
            event_id=event_id,
            status=(
                "completed"
                if row["item_id"] is not None or row["frame_done"]
                else "retrying"
                if retry_at is not None
                else "pending"
            ),
            attempts=row["attempts"],
            error_type=row["error_type"],
            retry_at=datetime.fromtimestamp(retry_at, UTC) if retry_at is not None else None,
            item_id=row["item_id"],
        )

    def list_slice_stream(
        self,
        *,
        user_id: str,
        day: str,
        timezone: str,
        conversation_id: str | None = None,
        limit: int = 100,
        as_of: datetime | None = None,
    ) -> tuple[SliceStreamItem, ...]:
        """按发生时间倒序读取指定日的全局及本会话切片流。"""
        self._store._ensure_open()
        self._store._check_scope(conversation_id, user_id)
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("day must use YYYY-MM-DD")
        ZoneInfo(timezone)
        where = (
            "user_id=? AND day=? AND timezone=? AND (conversation_scope='' OR conversation_scope=?)"
        )
        params: list[Any] = [user_id, day, timezone, conversation_id or ""]
        if as_of is not None:
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
            where += " AND ended_at<=? AND created_at<=?"
            params.extend((as_of.timestamp(), as_of.timestamp()))
        previous = "v.previous_id=s.item_id"
        if as_of is not None:
            previous += " AND v.known_at<=?"
            where += (
                " AND NOT EXISTS (SELECT 1 FROM slice_revisions v "
                "WHERE v.replacement_id=s.item_id AND v.known_at>?)"
            )
            params.append(as_of.timestamp())
            params.append(as_of.timestamp())
        where += f" AND NOT EXISTS (SELECT 1 FROM slice_revisions v WHERE {previous})"
        rows = self._store._connection.execute(
            f"SELECT data FROM slice_stream s WHERE {where} "
            "ORDER BY ended_at DESC,started_at DESC,seq DESC LIMIT ?",
            (*params, self._store._limit(limit)),
        ).fetchall()
        return tuple(SliceStreamItem.model_validate_json(row["data"]) for row in rows)
