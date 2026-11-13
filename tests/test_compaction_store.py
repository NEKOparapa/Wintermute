"""摘要、已读边界、摘要器取消和租约接管之间的存储约束。"""

import sqlite3
from pathlib import Path

import pytest

from wintermute.agent import AssistantMessage
from wintermute.agent.context.types import ContextCheckpoint, history_digest
from wintermute.events import EventError, EventInput, SQLiteStore, Terminal


def begin(store: SQLiteStore, text: str) -> str:
    receipt = store.append(
        EventInput(
            layer="conversation",
            type="user.message",
            terminal_id="phone",
            reply_terminal_id="phone",
            conversation_id="work",
            producer_event_id=text,
            payload={"text": text},
        )
    )
    job = store.claim_next()
    assert job is not None and receipt.job_id == job.job_id
    return job.job_id


def test_checkpoints_survive_restart_and_only_advance_on_verified_history(tmp_path: Path) -> None:
    path = tmp_path / "meeting.db"
    with SQLiteStore(path) as store:
        store.register_terminal(Terminal(terminal_id="phone", user_id="alice"))
        store.register_conversation("work", "alice")
        store.register_conversation("other", "bob")
        store.acquire_scheduler("one")
        run = begin(store, "先核对清单")
        initial = store.get_run_messages(run)
        checkpoint = ContextCheckpoint("核对会议清单", 1, history_digest(initial), 1)
        with pytest.raises(EventError, match="exceeds stored"):
            store.record_model_read(
                run, 10, checkpoint, messages=(*initial, AssistantMessage("尚未落盘的片段"))
            )
        assert store.get_run_messages(run) == initial
        assert store.load_model_read_count("work", "alice") == 0
        assert store.load_context_checkpoint("work", "alice") is None
        store.record_model_read(run, 1, checkpoint, messages=initial)
        assert store.try_finish_reply(
            run, messages=(*initial, AssistantMessage("已确认")), response="已确认"
        )
        with pytest.raises(EventError, match="no longer active"):
            store.record_model_read(run, 1, checkpoint, messages=initial)

    with SQLiteStore(path) as store:
        assert store.load_context_checkpoint("work", "alice") == checkpoint
        assert store.load_model_read_count("work", "alice") == 1
        assert store.load_context_checkpoint("other", "bob") is None
        with pytest.raises(EventError, match="different user"):
            store.load_context_checkpoint("work", "bob")
        store.acquire_scheduler("two")
        run = begin(store, "先别改")
        addition = store.get_run_messages(run)
        history = store.history("work", "alice")
        edited_prefix = ContextCheckpoint("摘要来自另一段历史", 2, "b" * 64, 2)
        store.record_model_read(run, 3, edited_prefix, messages=addition)
        assert store.load_model_read_count("work", "alice") == 3
        assert store.load_context_checkpoint("work", "alice") == checkpoint
        valid = ContextCheckpoint("已确认清单", 2, history_digest(history[:2]), 2)
        store.record_model_read(run, 3, valid, messages=addition)
        smaller = ContextCheckpoint("已核对", 2, valid.prefix_digest, 3)
        store.record_model_read(run, 3, smaller, messages=addition)
        store.record_model_read(run, 3, valid, messages=addition)
        stale = ContextCheckpoint("迟到的摘要", 2, valid.prefix_digest, 4)
        store.record_model_read(run, 2, stale, messages=addition)
        store.record_model_read(run, 2, checkpoint, messages=addition)
        assert store.load_context_checkpoint("work", "alice") == smaller
        assert store.load_model_read_count("work", "alice") == 3
        assert store.history("work", "alice") == history


def test_v6_migration_leaves_inputs_after_last_completed_run_unread(tmp_path: Path) -> None:
    path = tmp_path / "v6.db"
    with SQLiteStore(path) as store:
        store.register_conversation("work", "alice")
        store.register_terminal(Terminal(terminal_id="phone", user_id="alice"))
        store.acquire_scheduler("worker")
        run = begin(store, "读清单")
        assert store.try_finish_reply(
            run, messages=(*store.get_run_messages(run), AssistantMessage("完成")), response="完成"
        )
        later = begin(store, "先别改")
        store.fail_job(later, "进程中断", interrupted=True)
        old_history = store.history("work", "alice")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE session_model_state")
        db.execute("PRAGMA user_version=6")
    with SQLiteStore(path) as store:
        assert store.history("work", "alice") == old_history
        assert store.load_model_read_count("work", "alice") == 2
        assert store.load_context_checkpoint("work", "alice") is None
