import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from wintermute.events import SQLiteStore, Terminal
from wintermute.events.core.types import EventError, event_message
from wintermute.events.storage.remote_tools import RemoteCallConflict

NOW = datetime(2026, 11, 17, tzinfo=UTC)
ROOM = {"user_id": "owner", "conversation_id": "work"}
TARGET = {**ROOM, "terminal_id": "desktop"}
INPUT = {
    **TARGET,
    "tool": "shell",
    "arguments": {"command": "whoami"},
    "timeout_seconds": 10,
    "run_id": "run",
    "call_id": "call",
    "reply_terminal_id": "phone",
}


def register(store):
    store.register_conversation("work", "owner")
    store.register_conversation("other-room", "owner")
    for terminal in ("desktop", "phone"):
        store.register_terminal(Terminal(terminal_id=terminal, user_id="owner"))
    store.register_terminal(Terminal(terminal_id="foreign", user_id="someone-else"))


def test_competing_claims_restart_and_scoped_receipts(tmp_path):
    path = tmp_path / "remote.db"
    with SQLiteStore(path, clock=lambda: NOW) as old:
        register(old)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE remote_tool_calls")
        db.execute("PRAGMA user_version=18")
    with SQLiteStore(path, clock=lambda: NOW) as store:
        queue = store.remote_tools
        entry = queue.create(**INPUT)
        assert queue.create(**INPUT, available=False) == entry
        with pytest.raises(ValueError, match="list_targets"):
            queue.create(**{**INPUT, "call_id": "new"}, available=False)
        with pytest.raises(RemoteCallConflict):
            queue.create(**{**INPUT, "arguments": {"command": "whoami /all"}})
        with pytest.raises(ValueError):
            queue.create(**{**INPUT, "terminal_id": "foreign"})
        for extra in (
            {"tools": ["camera"]},
            {"terminal_id": "phone"},
            {"conversation_id": "other-room"},
        ):
            assert queue.claim(**{**TARGET, "tools": ["shell"], **extra}) is None
    barrier = Barrier(2)

    def claim():
        with SQLiteStore(path, clock=lambda: NOW) as store:
            barrier.wait(timeout=5)
            return store.remote_tools.claim(**TARGET, tools=["shell"])

    with ThreadPoolExecutor(2) as pool:
        claims = [pool.submit(claim) for _ in range(2)]
        issued = [claim for job in claims if (claim := job.result(timeout=10)) is not None]
    assert len(issued) == 1
    credential = issued[0].claim_token
    with SQLiteStore(path, clock=lambda: NOW) as store:
        queue = store.remote_tools
        receipt = dict(
            **TARGET,
            id=entry.id,
            claim_token=credential,
            status="succeeded",
            stdout="workstation\\user",
            exit_code=0,
        )
        for extra in (
            {"claim_token": "fake"},
            {"terminal_id": "phone"},
            {"conversation_id": "other-room"},
        ):
            with pytest.raises(LookupError):
                queue.complete(**{**receipt, **extra})
        # A failed notification write must not leave a finished call with no result for the Agent.
        store._connection.execute(
            "CREATE TRIGGER fail_notify BEFORE INSERT ON notification_events "
            "BEGIN SELECT RAISE(ABORT,'fail test'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            queue.complete(**receipt)
        assert queue.get(**ROOM, id=entry.id).status == "running"
        store._connection.execute("DROP TRIGGER fail_notify")
        done = queue.complete(**receipt)
        assert queue.complete(**receipt) == done
        with pytest.raises(RemoteCallConflict):
            queue.complete(**{**receipt, "stdout": "other output"})
        (notification,) = store.list_events("notification", user_id="owner")
        assert notification.reply_terminal_id == "phone"
        assert (
            done.status == "succeeded"
            and done.exit_code == 0
            and done.stdout in notification.payload["text"]
        )
        assert notification.producer_event_id == entry.id
        assert (
            credential not in done.model_dump_json()
            and credential not in notification.model_dump_json()
        )
        message = event_message(notification)
        assert (
            message.origin == "后台通知 · 终端工具" and message.text == notification.payload["text"]
        )


def test_due_calls_expire_without_reexecution_and_keep_ordered_history(tmp_path):
    path = tmp_path / "timeout.db"
    now = NOW
    with SQLiteStore(path, clock=lambda: now) as store:
        register(store)
        first = store.remote_tools.create(**INPUT)
        claim = store.remote_tools.claim(**TARGET, tools=["shell"])
        second = store.remote_tools.create(**{**INPUT, "call_id": "second"})
    now += timedelta(seconds=50)
    with SQLiteStore(path, clock=lambda: now) as store:
        queue = store.remote_tools
        assert queue.claim(**TARGET, tools=["shell"]) is None
        with pytest.raises(RemoteCallConflict):
            queue.complete(**TARGET, id=first.id, claim_token=claim.claim_token, status="failed")
        assert queue.expire() == 1 and queue.expire() == 0
        expired = queue.get(**ROOM, id=first.id)
        assert expired.status == "expired" and "未知" in expired.stderr
        assert "未执行" in queue.get(**ROOM, id=second.id).stderr
        assert len(store.list_events("notification", user_id="owner")) == 2
        items, cursor = queue.list(**ROOM, limit=1)
        assert [item.id for item in items] == [second.id] and cursor
        queue.create(**{**INPUT, "call_id": "third"})
        assert queue.list(**ROOM, limit=1, cursor=cursor) == ([expired], None)
        assert queue.list(user_id="someone-else", conversation_id="work") == ([], None)
        with pytest.raises(ValueError):
            queue.list(**ROOM, cursor="9999999999999999999")


def test_small_event_budget_keeps_output_retrievable_without_dispatching_undeliverable_work():
    with SQLiteStore(":memory:", max_event_bytes=1) as store:
        register(store)
        with pytest.raises(EventError, match="byte limit"):
            store.remote_tools.create(**INPUT)
        assert store.remote_tools.list(**ROOM) == ([], None)
    with SQLiteStore(":memory:", max_event_bytes=1024) as store:
        register(store)
        call = store.remote_tools.create(**INPUT)
        claim = store.remote_tools.claim(**TARGET, tools=["shell"])
        full_output = "工" * 2000
        store.remote_tools.complete(
            **TARGET,
            id=call.id,
            claim_token=claim.claim_token,
            status="succeeded",
            stdout=full_output,
            exit_code=0,
        )
        (notice,) = store.list_events("notification", user_id="owner")
        assert call.id in notice.payload["text"] and full_output not in notice.payload["text"]
        assert store.remote_tools.get(**ROOM, id=call.id).stdout == full_output
