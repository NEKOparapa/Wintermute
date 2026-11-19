"""定时任务及其原子事件投递。"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from wintermute.events.core.schedule import (
    ScheduleAction,
    ScheduleEntry,
    ScheduleStatus,
    due_notification,
)
from wintermute.events.core.types import (
    Terminal,
)
from wintermute.events.storage import schedules

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class ScheduleStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def create_schedule(
        self,
        *,
        user_id: str,
        conversation_id: str,
        title: str,
        trigger_at: datetime,
        content: str = "",
        action: ScheduleAction = "notify_agent",
    ) -> ScheduleEntry:
        entry = ScheduleEntry(
            id=f"sch_{uuid4().hex}",
            title=title,
            content=content,
            trigger_at=trigger_at,
            action=action,
        )
        notification = due_notification(entry, user_id, conversation_id)
        self._store._check_event_size(notification)
        with self._store._transaction():
            self._store._check_scope(conversation_id, user_id)
            self._store._register_terminal(
                Terminal(
                    terminal_id=notification.terminal_id,
                    user_id=user_id,
                    allowed_layers=("notification",),
                )
            )
            self._store._connection.execute(
                "INSERT INTO schedules(id,user_id,conversation_id,title,content,action,trigger_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    entry.id,
                    user_id,
                    conversation_id,
                    entry.title,
                    entry.content,
                    entry.action,
                    schedules.stamp(entry.trigger_at),
                ),
            )
        return entry

    def get_schedule(self, *, user_id: str, conversation_id: str, id: str) -> ScheduleEntry:
        self._store._check_scope(conversation_id, user_id)
        row = self._store._connection.execute(
            f"SELECT {schedules.FIELDS} FROM schedules "
            "WHERE user_id=? AND conversation_id=? AND id=?",
            (user_id, conversation_id, id),
        ).fetchone()
        if row is None:
            raise LookupError("找不到这条日程")
        return ScheduleEntry.model_validate(dict(row))

    def update_schedule(
        self,
        *,
        user_id: str,
        conversation_id: str,
        id: str,
        title: str | None = None,
        content: str | None = None,
        trigger_at: datetime | None = None,
        action: ScheduleAction | None = None,
    ) -> ScheduleEntry:
        with self._store._transaction():
            before = self.get_schedule(user_id=user_id, conversation_id=conversation_id, id=id)
            if before.status != "scheduled":
                raise ValueError("已触发的日程无法修改")
            if title is None and content is None and trigger_at is None and action is None:
                raise ValueError("请指定要修改的日程字段")
            entry = ScheduleEntry(
                id=id,
                title=before.title if title is None else title,
                content=before.content if content is None else content,
                trigger_at=before.trigger_at if trigger_at is None else trigger_at,
                action=before.action if action is None else action,
            )
            self._store._check_event_size(due_notification(entry, user_id, conversation_id))
            self._store._connection.execute(
                "UPDATE schedules SET title=?,content=?,action=?,trigger_at=? WHERE id=?",
                (entry.title, entry.content, entry.action, schedules.stamp(entry.trigger_at), id),
            )
            return entry

    def delete_schedule(self, *, user_id: str, conversation_id: str, id: str) -> ScheduleEntry:
        with self._store._transaction():
            entry = self.get_schedule(user_id=user_id, conversation_id=conversation_id, id=id)
            self._store._connection.execute("DELETE FROM schedules WHERE id=?", (id,))
            return entry

    def list_schedules(
        self,
        *,
        user_id: str,
        conversation_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        status: ScheduleStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[ScheduleEntry], str | None]:
        self._store._check_scope(conversation_id, user_id)
        return schedules.list_entries(
            self._store._connection, user_id, conversation_id, start, end, status, limit, cursor
        )

    def has_due_schedules(self) -> bool:
        self._store._ensure_open()
        return (
            self._store._connection.execute(
                "SELECT 1 FROM schedules WHERE triggered_at IS NULL AND trigger_at<=? LIMIT 1",
                (schedules.stamp(self._store._now()),),
            ).fetchone()
            is not None
        )

    def dispatch_due_schedules(self, *, limit: int = 32, owner_id: str | None = None) -> int:
        """Commit due notifications, their dispatch jobs and calendar state in one transaction."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Schedule dispatch limit must be between 1 and 100")
        if not self.has_due_schedules():
            return 0
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            now = schedules.stamp(self._store._now())
            rows = self._store._connection.execute(
                f"SELECT user_id,conversation_id,{schedules.FIELDS} FROM schedules "
                "WHERE triggered_at IS NULL AND trigger_at<=? ORDER BY trigger_at,id LIMIT ?",
                (now, limit),
            ).fetchall()
            for row in rows:
                fields = dict(row)
                user_id, conversation_id = fields.pop("user_id"), fields.pop("conversation_id")
                entry = ScheduleEntry.model_validate(fields)
                self._store._append_event(due_notification(entry, user_id, conversation_id))
                self._store._connection.execute(
                    "UPDATE schedules SET triggered_at=? WHERE id=?", (now, entry.id)
                )
            return len(rows)
