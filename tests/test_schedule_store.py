"""Persistent calendars, scoped reads and atomic dispatch after restart."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from wintermute.events import EventInput, SchedulerBusyError, SQLiteStore, Terminal

NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
ROOM = {"user_id": "owner", "conversation_id": "work"}


def test_v11_calendar_migration_order_scope_and_edits(tmp_path):
    path = tmp_path / "calendar.db"
    with SQLiteStore(path) as old:
        old.register_conversation("work", "owner")
        old.register_conversation("other", "owner")
        old.register_terminal(Terminal(terminal_id="desktop", user_id="owner"))
        original = old.append(
            EventInput(
                layer="conversation",
                type="user.message",
                terminal_id="desktop",
                conversation_id="work",
                producer_event_id="before-migration",
                payload={"text": "原会话"},
                occurred_at=NOW,
            )
        )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE schedules")
        db.execute("PRAGMA user_version=11")
    with SQLiteStore(path, clock=lambda: NOW) as store:
        assert store.get_event(original.event.event_id) == original.event
        later = NOW + timedelta(hours=2)
        start = NOW.astimezone(ZoneInfo("Asia/Shanghai"))
        entries = [
            store.create_schedule(**ROOM, title=title, trigger_at=start, content="detail")
            for title in ("one", "two", "three")
        ]
        moved = store.update_schedule(**ROOM, id=entries[1].id, trigger_at=later, content="")
        assert moved.title == "two" and moved.content == "" and moved.trigger_at == later
        expected = sorted([entries[0], entries[2]], key=lambda item: item.id)
        first, cursor = store.list_schedules(**ROOM, start=start, end=later, limit=1)
        assert first == expected[:1] and cursor is not None
        assert store.delete_schedule(**ROOM, id=first[0].id) == first[0]
        second, end_cursor = store.list_schedules(**ROOM, start=start, end=later, cursor=cursor)
        assert second == expected[1:] and end_cursor is None
        assert store.list_schedules(user_id="owner", conversation_id="other") == ([], None)
        with pytest.raises(LookupError):
            store.delete_schedule(user_id="owner", conversation_id="other", id=moved.id)
        with pytest.raises(ValueError):
            store.list_schedules(user_id="stranger", conversation_id="work")
        assert store.list_schedules(**ROOM, start=later) == ([moved], None)
        with pytest.raises(ValueError):
            store.list_schedules(**ROOM, start=NOW.replace(tzinfo=None))
        with pytest.raises(ValueError):
            store.list_schedules(**ROOM, cursor="invalid")


def test_overdue_dispatch_rollback_two_connections_and_history_after_delete(tmp_path):
    path = tmp_path / "calendar.db"
    later = NOW + timedelta(hours=2)
    with SQLiteStore(path, clock=lambda: NOW) as store:
        store.register_conversation("work", "owner")
        entry = store.create_schedule(
            **ROOM, title="Review", trigger_at=later, content="Bring notes"
        )
        assert not store.has_due_schedules() and store.dispatch_due_schedules() == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TRIGGER fail_mark BEFORE UPDATE ON schedules "
            "BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with SQLiteStore(path, clock=lambda: later) as store:
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            store.dispatch_due_schedules()
        assert store.has_due_schedules() and not store.list_events("notification", user_id="owner")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TRIGGER fail_mark")
        assert db.execute("SELECT COUNT(*) FROM dispatch_jobs").fetchone() == (0,)

    barrier = Barrier(2, timeout=6)
    resumed = later + timedelta(minutes=3)

    def dispatch():
        with SQLiteStore(path, clock=lambda: resumed) as store:
            barrier.wait()
            return store.dispatch_due_schedules()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(dispatch) for _ in range(2)]
        assert sorted(job.result() for job in results) == [0, 1]
    with SQLiteStore(path, clock=lambda: resumed) as store:
        (event,) = store.list_events("notification", user_id="owner")
        assert (event.type, event.correlation_id, event.reply_terminal_id) == (
            "schedule.due",
            entry.id,
            None,
        )
        assert event.received_at == resumed and event.occurred_at == later
        assert event.payload["content"] == entry.content and event.conversation_id == "work"
        (fired,), cursor = store.list_schedules(**ROOM, status="triggered")
        assert fired.triggered_at == resumed and cursor is None
        with pytest.raises(ValueError, match="已触发"):
            store.update_schedule(**ROOM, id=fired.id, title="Invalid")
        assert store.delete_schedule(**ROOM, id=fired.id) == fired
        assert store.dispatch_due_schedules() == 0
        assert store.get_event(event.event_id) == event
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (1,)


def test_oversized_schedule_cannot_poison_due_dispatch():
    with SQLiteStore(clock=lambda: NOW, max_event_bytes=1200) as store:
        store.register_conversation("work", "owner")
        with pytest.raises(ValueError, match="byte limit"):
            store.create_schedule(**ROOM, title="Review", trigger_at=NOW, content="会议" * 300)
        accepted = store.create_schedule(**ROOM, title="Review", trigger_at=NOW)
        with pytest.raises(ValueError, match="byte limit"):
            store.update_schedule(**ROOM, id=accepted.id, content="会议" * 300)
        assert store.get_schedule(**ROOM, id=accepted.id) == accepted
        assert store.dispatch_due_schedules() == 1


def test_lease_takeover_dispatches_previously_accepted_content_under_lower_limit(tmp_path):
    path = tmp_path / "calendar.db"
    now = NOW
    with SQLiteStore(path, clock=lambda: now) as previous:
        previous.register_conversation("work", "owner")
        previous.acquire_scheduler("old", lease_seconds=5)
        entry = previous.create_schedule(
            **ROOM, title="Review", content="x" * 1800, trigger_at=now + timedelta(seconds=10)
        )
        now += timedelta(seconds=11)
        with SQLiteStore(path, clock=lambda: now, max_event_bytes=1000) as replacement:
            replacement.acquire_scheduler("new")
            with pytest.raises(SchedulerBusyError):
                previous.dispatch_due_schedules(owner_id="old")
            assert replacement.dispatch_due_schedules(owner_id="new") == 1
            (notification,) = replacement.list_events("notification", user_id="owner")
            assert notification.payload["content"] == entry.content
            assert replacement.dispatch_due_schedules(owner_id="new") == 0
