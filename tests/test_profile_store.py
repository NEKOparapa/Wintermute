"""Durable user evidence and publication checkpoints across independent DB connections."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from wintermute.events import EventInput, SQLiteStore, Terminal

NOW = datetime(2026, 11, 16, 1, tzinfo=UTC)


async def test_usage_reader_opens_the_right_database_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    from wintermute.events.storage import sqlite

    calls = []
    usage = sqlite.activity_pattern

    def traced(db, user_id, now, timezone):
        calls.append(threading.get_ident())
        return usage(db, user_id, now, timezone)

    monkeypatch.setattr(sqlite, "activity_pattern", traced)
    with SQLiteStore(tmp_path / "档案#2.sqlite3") as disk, SQLiteStore(":memory:") as memory:
        assert await disk.read_profile_activity("me", NOW, "Asia/Shanghai") == (
            await memory.read_profile_activity("me", NOW, "Asia/Shanghai")
        )
    assert calls[0] != calls[1] == threading.get_ident()


def register(store, name):
    store.register_terminal(Terminal(terminal_id=name, user_id=name))
    store.register_conversation(name, name)


def append(store, user, i, text, *, notice=False, occurred_at=NOW):
    return store.append(
        EventInput(
            terminal_id=user,
            conversation_id=user,
            producer_event_id=i,
            layer="notification" if notice else "conversation",
            type="schedule.due" if notice else "user.message",
            payload={"text": text},
            occurred_at=occurred_at,
        )
    )


def finish(store, text):
    job = store.claim_next()
    assert job is not None
    store.finish_job(job.job_id, messages=(), response=text, reply_terminal_id=None)


def proposal(status, through=None):
    return {
        "cursor": status.cursor,
        "through": status.latest_seq if through is None else through,
        "latest_seq": status.latest_seq,
        "path": "/profiles/me/PROFILE.md",
        "base_digest": "test-digest",
        "content": "项目周会材料发手机。",
    }


def test_v14_dialogue_keeps_user_evidence_receive_order_and_local_assistant_context(tmp_path):
    path = tmp_path / "events.sqlite3"
    with SQLiteStore(path, clock=lambda: NOW) as old:
        register(old, "owner")
        register(old, "guest")
        old.acquire_scheduler("before")
        first = append(old, "owner", "1", "周会材料发手机")
        finish(old, "只用于项目周会？")
        append(old, "guest", "1", "其他用户的原话")
        finish(old, "其他用户的回复")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE profile_progress")
        db.execute("PRAGMA user_version=14")
    now = NOW + timedelta(days=1)
    with SQLiteStore(path, clock=lambda: now) as store:
        store.acquire_scheduler("owner")
        late = append(store, "owner", "2", "是的", occurred_at=NOW - timedelta(days=4))
        assert store.profile_status("owner").busy
        finish(store, "收到")
        append(store, "owner", "timer", "用户爱玩游戏", notice=True)
        finish(store, "后台回复")
        append(store, "owner", "3", "关联的通知不提供证据")
        finish(store, "收到")
        state = store.profile_status("owner")
        assert not state.busy and state.cursor == 0
        assert state.now == state.latest_received_at == now
        assert state.oldest_pending_at == NOW
        inputs = store.profile_inputs("owner", after=0, through=state.latest_seq)
        assert [i.text for i in inputs] == ["周会材料发手机", "是的", "关联的通知不提供证据"]
        assert [i.previous_reply for i in inputs] == [None, "只用于项目周会？", None]
        assert [i.event_id for i in inputs[:2]] == [first.event.event_id, late.event.event_id]
        assert inputs[1].occurred_at < inputs[0].occurred_at
        assert (
            store.profile_inputs("owner", after=0, through=state.latest_seq, limit=1) == inputs[:1]
        )


def test_staged_work_rejects_new_input_recovers_published_file_and_advances_once(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with SQLiteStore(path) as first, SQLiteStore(path) as ingress:
        register(first, "owner")
        first.acquire_scheduler("worker")
        append(ingress, "owner", "1", "以后发手机")
        busy = first.profile_status("owner")
        assert not first.stage_profile("owner", **proposal(busy), owner_id="worker")
        finish(first, "已接收")
        assert first.stage_profile("owner", **proposal(busy), owner_id="worker")
        stale = first.profile_status("owner").pending
        append(ingress, "owner", "2", "改发电脑")
        assert not first.settle_profile(
            "owner", stale, advance=True, publish=lambda: pytest.fail("stale publish")
        )
        assert first.profile_status("owner").pending is None
        finish(first, "收到")
        state = first.profile_status("owner")
        assert not first.stage_profile("owner", **proposal(busy))
        assert first.stage_profile("owner", **proposal(state))
        intended = first.profile_status("owner").pending

        def crash():
            (tmp_path / "published.md").write_text(intended.content, encoding="utf-8")
            raise OSError("crash after rename")

        with pytest.raises(OSError, match="crash after rename"):
            first.settle_profile("owner", intended, advance=True, publish=crash)
    with SQLiteStore(path) as resumed:
        recovered = resumed.profile_status("owner")
        assert recovered.pending == intended and recovered.cursor == 0
        assert (tmp_path / "published.md").read_text(encoding="utf-8") == intended.content
        assert resumed.settle_profile("owner", recovered.pending, advance=True)
        assert not resumed.settle_profile("owner", recovered.pending, advance=True)
        ready = resumed.profile_status("owner")
        assert ready.oldest_pending_at is None and ready.cursor == state.latest_seq
        assert resumed.stage_profile("owner", **proposal(ready))
        assert resumed.settle_profile(
            "owner", resumed.profile_status("owner").pending, advance=False
        )
