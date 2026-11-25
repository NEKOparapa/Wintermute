"""面向设备的历史、来源和压缩过程查询。"""

from typing import TYPE_CHECKING, Any

from purin.events.storage import compression_history, compression_sources, perception_trace
from purin.events.storage.history import (
    HistoryItem,
    browse,
    screenshot_frames,
)
from purin.events.storage.read_snapshot import read_snapshot

if TYPE_CHECKING:
    from purin.events.storage.sqlite import SQLiteStore


class HistoryStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

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
