"""活动投影、合并时间线和压缩工作队列。"""

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from wintermute.agent.core.types import (
    UserMessage,
)
from wintermute.events.core.activity import (
    ActivityItem,
    timeline_line,
)
from wintermute.events.core.perception_rules import (
    SHORT_DWELL_SECONDS,
    perception_line,
)
from wintermute.events.core.presentation import body_value, timestamp_text
from wintermute.events.storage import merged_timeline, perception_buffer, timeline_batches
from wintermute.events.storage.codec import (
    encode_json,
)

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class TimelineStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def _project_activity(
        self,
        item: ActivityItem,
        *,
        source_seq: int | None = None,
    ) -> None:
        """在接收或摘要提交事务内比较每个会话最近收到的同类活动。"""
        content = encode_json(body_value(item.content))
        if item.valid_until is not None:
            content = encode_json([body_value(item.content), timestamp_text(item.valid_until)])
        head = self._store._connection.execute(
            "SELECT source_seq,occurred_at,content FROM activity_heads "
            "WHERE user_id=? AND activity_key=?",
            (item.user_id, item.activity_key),
        ).fetchone()
        instant = item.occurred_at.timestamp()
        if head is not None and item.kind == "perception":
            if source_seq is not None and head["source_seq"] is not None:
                if source_seq <= head["source_seq"]:
                    return
            elif instant < head["occurred_at"]:
                return
        changed = head is None or head["content"] != content
        self._store._connection.execute(
            "INSERT INTO activity_heads(user_id,activity_key,source_seq,occurred_at,content) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,activity_key) DO UPDATE SET "
            "source_seq=excluded.source_seq,occurred_at=excluded.occurred_at,"
            "content=excluded.content",
            (item.user_id, item.activity_key, source_seq, instant, content),
        )
        targets = self._store._connection.execute(
            "SELECT c.conversation_id FROM conversations c WHERE user_id=? "
            "AND (? IS NULL OR c.conversation_id=?) AND (SELECT i.content "
            "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
            "WHERE d.conversation_id=c.conversation_id AND d.activity_key=? "
            "ORDER BY d.activity_seq DESC LIMIT 1) IS NOT ?",
            (item.user_id, item.conversation_id, item.conversation_id, item.activity_key, content),
        ).fetchall()
        if not changed and not targets:
            return
        cursor = self._store._connection.execute(
            "INSERT INTO activity_items(activity_id,after_receipt,user_id,activity_key,occurred_at,"
            "content,data,canonical_change) VALUES(?,(SELECT COALESCE(MAX(seq),0) FROM receipts),"
            "?,?,?,?,?,?)",
            (
                item.activity_id,
                item.user_id,
                item.activity_key,
                instant,
                content,
                item.model_dump_json(),
                changed,
            ),
        )
        for target in targets:
            conversation = target["conversation_id"]
            self._store._connection.execute(
                "INSERT INTO activity_deliveries(conversation_id,activity_seq,activity_key) "
                "VALUES(?,?,?)",
                (conversation, cursor.lastrowid, item.activity_key),
            )
        assert cursor.lastrowid is not None
        if item.kind == "perception":
            line, threshold, deduct_short = self._perception_line(item)
        else:
            line, threshold, deduct_short = timeline_line(item), SHORT_DWELL_SECONDS, True
        perception_buffer.append(
            self._store._connection, cursor.lastrowid, item, line, threshold, deduct_short
        )
        for target in targets:
            room = target["conversation_id"]
            merged_timeline.add(self._store._connection, room, cursor.lastrowid, item, line)
            if line:
                merged_timeline.prepare(self._store._connection, room, self._store.timeline_policy)

    def _perception_line(self, item: ActivityItem) -> tuple[str, int, bool]:
        source = (
            self._store.get_event(item.source_event_ids[0])
            if item.event_type == "desktop.visual" and isinstance(item.content, str)
            else None
        )
        threshold = SHORT_DWELL_SECONDS
        deduct_short = True
        if source:
            stored = self._store._connection.execute(
                "SELECT m.dwell_threshold,m.deduct_short FROM activity_items i "
                "JOIN timeline_batch_members m "
                "ON m.activity_seq=i.seq WHERE i.activity_id=?",
                (source.event_id,),
            ).fetchone()
            if stored:
                threshold, deduct_short = stored[0], bool(stored[1])
        line = perception_line(
            item,
            source,
            threshold=threshold,
            deduct_short=deduct_short,
            zone=ZoneInfo(self._store.timeline_policy.timezone),
        )
        return line, threshold, deduct_short

    def pending_timeline(self, conversation_id: str, user_id: str) -> UserMessage | None:
        """预览待发送时间线的变化，不确认投递或修改已有历史。"""
        self._store._ensure_open()
        standalone = not self._store._connection.in_transaction
        if standalone:
            self._store._connection.execute("BEGIN")
        try:
            self._store._check_scope(conversation_id, user_id)
            return timeline_batches.pending(
                self._store._connection, conversation_id, user_id, self._store.timeline_policy
            )
        finally:
            if standalone:
                self._store._connection.rollback()  # release the read snapshot

    def next_timeline_batch(self) -> timeline_batches.TimelineBatch | None:
        with self._store._transaction():
            row = self._store._connection.execute(
                "SELECT id,attempts FROM merged_batches WHERE ai IS NULL AND tokens>=? AND "
                "retry_at<=? "
                "ORDER BY retry_at,id LIMIT 1",
                (self._store.timeline_policy.ai_threshold, self._store._now().timestamp()),
            ).fetchone()
            if row is None:
                return None
            lines = self._store._connection.execute(
                "SELECT line FROM merged_entries WHERE batch=? ORDER BY at,seq", (row["id"],)
            )
            return timeline_batches.TimelineBatch(
                row["id"],
                "\n".join(r[0] for r in lines),
                row["attempts"],
                min(
                    self._store.timeline_policy.summary_tokens,
                    self._store.timeline_policy.delivery_tokens // 2,
                ),
            )

    def finish_timeline_batch(self, batch_id: int, text: str) -> None:
        if not text.strip():
            raise ValueError("Timeline summary must not be empty")
        with self._store._transaction():
            self._store._connection.execute(
                "UPDATE merged_batches SET ai=?,generated_at=? WHERE id=? AND ai IS NULL",
                (
                    merged_timeline.excerpt(
                        text.strip(),
                        min(
                            self._store.timeline_policy.summary_tokens,
                            self._store.timeline_policy.delivery_tokens // 2,
                        ),
                    ),
                    round(self._store._now().timestamp() * 1_000_000),
                    batch_id,
                ),
            )

    def retry_timeline_batch(self, batch_id: int, *, delay_seconds: float) -> None:
        with self._store._transaction():
            self._store._connection.execute(
                "UPDATE merged_batches SET attempts=attempts+1,retry_at=? WHERE id=? AND ai "
                "IS NULL",
                (self._store._now().timestamp() + delay_seconds, batch_id),
            )

    def list_activity(
        self, *, user_id: str, conversation_id: str | None = None, limit: int = 100
    ) -> tuple[ActivityItem, ...]:
        """最新 N 个变化按发生时间正序返回；可按会话查询其实际投递范围。"""
        self._store._ensure_open()
        self._store._check_scope(conversation_id, user_id)
        rows = self._store._connection.execute(
            "SELECT i.data FROM activity_items i WHERE i.user_id=? AND "
            "((? IS NULL AND i.canonical_change=1) OR EXISTS (SELECT 1 FROM activity_deliveries d "
            "WHERE d.activity_seq=i.seq AND d.conversation_id=?)) "
            "ORDER BY i.occurred_at DESC,i.seq DESC LIMIT ?",
            (user_id, conversation_id, conversation_id, self._store._limit(limit)),
        ).fetchall()
        return tuple(ActivityItem.model_validate_json(row["data"]) for row in reversed(rows))
