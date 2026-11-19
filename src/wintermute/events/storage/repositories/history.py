"""面向终端的历史、来源和压缩过程查询。"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from wintermute.events.core.activity import (
    ActivityItem,
)
from wintermute.events.storage import compression_history, compression_sources, perception_trace
from wintermute.events.storage.history import (
    HistoryItem,
    browse,
    screenshot_frames,
)
from wintermute.events.storage.read_snapshot import read_snapshot

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class HistoryStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def search_activity(
        self,
        *,
        user_id: str,
        conversation_id: str,
        start: datetime,
        end: datetime,
        terminal: str | None = None,
        keyword: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[tuple[ActivityItem, ...], bool]:
        """按发生时间正序检索本会话可见的原始感知和切片文字。"""
        self._store._check_scope(conversation_id, user_id)
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ValueError("Expected a nonempty timezone-aware time interval")
        if not 1 <= limit <= 20 or offset < 0:
            raise ValueError("Invalid activity page")
        rows = self._store._connection.execute(
            "SELECT i.data FROM activity_items i INDEXED BY activity_timeline "
            "JOIN activity_deliveries d ON d.activity_seq=i.seq AND d.conversation_id=? "
            "WHERE i.user_id=? AND i.occurred_at>=? AND i.occurred_at<? "
            "AND (? IS NULL OR json_extract(i.data,'$.terminal_id')=?) "
            "AND (? IS NULL OR instr(lower(i.content),lower(?))>0) "
            "AND NOT EXISTS(SELECT 1 FROM slice_revisions r WHERE r.previous_id=i.activity_id) "
            "ORDER BY i.occurred_at,i.seq LIMIT ? OFFSET ?",
            (
                conversation_id,
                user_id,
                start.timestamp(),
                end.timestamp(),
                terminal,
                terminal,
                keyword,
                keyword,
                limit + 1,
                offset,
            ),
        ).fetchall()
        items = tuple(ActivityItem.model_validate_json(r[0]) for r in rows[:limit])
        return items, len(rows) > limit

    def browse_history(
        self,
        *,
        user_id: str,
        conversation_id: str,
        view: str,
        cursor: str | None = None,
        limit: int = 50,
        grouped: bool = False,
    ) -> tuple[list[HistoryItem], str | None]:
        with read_snapshot(self._store._connection):
            self._store._check_scope(conversation_id, user_id)
            return browse(
                self._store._connection,
                user_id,
                conversation_id,
                view,
                cursor,
                limit,
                self._store.timeline_policy,
                grouped=grouped,
            )

    def browse_screenshot_group(
        self,
        *,
        user_id: str,
        conversation_id: str,
        group_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[HistoryItem], str | None]:
        self._store._check_scope(conversation_id, user_id)
        return screenshot_frames(
            self._store._connection, user_id, conversation_id, group_id, cursor, limit
        )

    def perception_stages(
        self, user_id: str, conversation_id: str, event_id: str, *, ai_enabled: bool
    ) -> dict[str, Any]:
        self._store._check_scope(conversation_id, user_id)
        return compression_history.perception_stages(
            self._store._connection,
            user_id,
            conversation_id,
            event_id,
            self._store.timeline_policy,
            ai_enabled,
        )

    def trace_members(
        self, user: str, conversation: str, target: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        self._store._check_scope(conversation, user)
        return perception_trace.members(
            self._store._connection,
            user,
            conversation,
            target,
            cursor,
            limit,
            self._store.timeline_policy,
        )

    def browse_compression_batches(
        self,
        *,
        user_id: str,
        conversation_id: str,
        layer: str,
        cursor: str | None,
        limit: int,
        ai_enabled: bool,
    ) -> dict[str, Any]:
        self._store._check_scope(conversation_id, user_id)
        return compression_history.browse_batches(
            self._store._connection,
            user_id,
            conversation_id,
            layer,
            cursor,
            limit,
            self._store.timeline_policy,
            ai_enabled,
        )

    def browse_compression_batch(
        self,
        *,
        user_id: str,
        conversation_id: str,
        batch_id: str,
        cursor: str | None,
        limit: int,
        ai_enabled: bool,
    ) -> dict[str, Any]:
        self._store._check_scope(conversation_id, user_id)
        return compression_history.batch_detail(
            self._store._connection,
            user_id,
            conversation_id,
            batch_id,
            cursor,
            limit,
            self._store.timeline_policy,
            ai_enabled,
        )

    def browse_compression_sources(
        self,
        *,
        user_id: str,
        conversation_id: str,
        source_id: str,
        activity: bool,
        cursor: str | None,
        limit: int,
    ) -> compression_sources.SourcePage:
        self._store._check_scope(conversation_id, user_id)
        return compression_sources.browse_sources(
            self._store._connection,
            user_id,
            conversation_id,
            source_id,
            activity=activity,
            cursor=cursor,
            limit=limit,
        )
