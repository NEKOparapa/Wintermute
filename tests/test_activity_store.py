"""Activity summaries remain separate from each conversation's delivery acknowledgements."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wintermute.agent import AssistantMessage
from wintermute.events import ActivityItem, EventGateway, SliceStreamItem, SQLiteStore, Terminal
from wintermute.events.core.timeline_compression import TimelinePolicy, estimate

BASE = datetime(2026, 5, 2, 10, tzinfo=UTC)


def setup(store: SQLiteStore) -> EventGateway:
    gateway = EventGateway(store)
    gateway.register_terminal(Terminal(terminal_id="pc", user_id="u"))
    gateway.register_conversation("work", "u")
    return gateway


async def read(gateway: EventGateway) -> None:
    store = gateway.store
    await gateway.conversation.connect("pc", "work").send("update?")
    store.acquire_scheduler("runner")
    job = store.claim_next(defer_input=True)
    assert job is not None
    store.absorb_session_inputs(job.job_id, messages=())
    assert store.try_finish_reply(
        job.job_id,
        messages=(*store.get_run_messages(job.job_id), AssistantMessage("ok")),
        response="ok",
    )


async def test_cache_requires_exact_conversation_coverage_and_idle_survives_restart(tmp_path: Path):
    now = BASE
    path = tmp_path / "coverage.db"
    policy = TimelinePolicy(ai_threshold=1, idle_seconds=60)
    with SQLiteStore(path, clock=lambda: now, timeline_policy=policy) as store:
        gateway = setup(store)
        device = gateway.perception.connect("pc")
        await device.observe("document", "EARLIER")
        assert store.next_timeline_batch() is None
        gateway.register_conversation("new", "u")
        await device.observe("document", "LATER")
        assert store.next_timeline_batch() is None
    now += timedelta(seconds=61)
    with SQLiteStore(path, clock=lambda: now, timeline_policy=policy) as store:
        frozen = store.next_timeline_batch()
        assert frozen is not None
        assert "EARLIER" in frozen.rule_text and "LATER" in frozen.rule_text
        store.finish_timeline_batch(frozen.batch_id, "WHOLE_GROUP")
        store.finish_timeline_batch(frozen.batch_id, "wrong overwrite")
        complete = store.pending_timeline("work", "u")
        partial = store.pending_timeline("new", "u")
        assert complete and "WHOLE_GROUP" in complete.text and "wrong" not in complete.text
        assert partial and "LATER" in partial.text
        assert "WHOLE_GROUP" not in partial.text and "EARLIER" not in partial.text
        assert store.next_timeline_batch() is None
        await read(EventGateway(store))
        assert store.pending_timeline("work", "u") is None
        assert store.pending_timeline("new", "u") == partial


async def test_late_observation_rebuilds_visible_suffix_including_v9_history(tmp_path: Path):
    path = tmp_path / "old.db"
    with SQLiteStore(path) as store:
        gateway = setup(store)
        device = gateway.perception.connect("pc", conversation_id="work")
        await device.observe("first", "FIRST_FILE", occurred_at=BASE)
        await device.observe("last", "LAST_FILE", occurred_at=BASE + timedelta(minutes=10))
        await read(gateway)
        old = store.history("work", "u")
        await device.observe("late", "LATE_FILE", occurred_at=BASE + timedelta(minutes=5))
    with sqlite3.connect(path) as db:
        for name in (
            "timeline_rollup_members",
            "timeline_rollups",
            "timeline_batch_members",
            "timeline_batches",
            "timeline_delivery_heads",
        ):
            db.execute(f"DROP TABLE {name}")
        db.execute("PRAGMA user_version=9")
    with SQLiteStore(path) as store:
        assert store.history("work", "u") == old
        update = store.pending_timeline("work", "u")
        assert update and "FIRST_FILE" not in update.text
        assert update.text.index("LATE_FILE") < update.text.index("LAST_FILE")
        await read(EventGateway(store))
        assert store.pending_timeline("work", "u") is None


async def test_slice_revision_invalidates_mixed_cache_and_preserves_suffix():
    with SQLiteStore(timeline_policy=TimelinePolicy(ai_threshold=1, raw_tokens=1)) as store:
        gateway = setup(store)
        source = await gateway.slice.connect("pc").record("transcript", occurred_at=BASE)
        item = SliceStreamItem(
            item_id="initial",
            user_id="u",
            terminal_id="pc",
            source_id="default",
            day="2026-05-02",
            timezone="UTC",
            started_at=BASE,
            ended_at=BASE,
            source_event_ids=(source.event.event_id,),
            text="INITIAL_TOPIC",
            strategy="rules",
            created_at=BASE,
        )
        store.commit_slice(item)
        initial = store.next_timeline_batch()
        assert initial
        store.finish_timeline_batch(initial.batch_id, "STALE_AI")
        later = gateway.perception.connect("pc")
        await later.observe("file", "LATER_FILE", occurred_at=BASE + timedelta(minutes=1))
        await read(gateway)
        store.commit_slice(
            item.model_copy(update={"item_id": "corrected", "text": "REVISED_TOPIC"})
        )
        update = store.pending_timeline("work", "u")
        assert update and "STALE_AI" not in update.text and "INITIAL_TOPIC" not in update.text
        assert update.text.index("REVISED_TOPIC") < update.text.index("LATER_FILE")


async def test_local_day_boundary_seals_pre_registration_work_without_delivering_it():
    before = BASE.replace(hour=15, minute=59)
    after = before + timedelta(minutes=2)
    with SQLiteStore(
        clock=lambda: after,
        timeline_policy=TimelinePolicy(ai_threshold=1, timezone="Asia/Shanghai"),
    ) as store:
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="pc", user_id="u"))
        device = gateway.perception.connect("pc")
        await device.observe("document", "YESTERDAY", occurred_at=before)
        gateway.register_conversation("work", "u")
        await device.observe("document", "TODAY", occurred_at=after)
        task = store.next_timeline_batch()
        assert task and "YESTERDAY" in task.rule_text and "TODAY" not in task.rule_text
        store.finish_timeline_batch(task.batch_id, "old summary")
        pending = store.pending_timeline("work", "u")
        assert pending and "TODAY" in pending.text
        assert "YESTERDAY" not in pending.text and "old summary" not in pending.text


async def test_long_backlog_can_render_from_batch_cache_without_raw_decoding(monkeypatch):
    with SQLiteStore(timeline_policy=TimelinePolicy(raw_tokens=120)) as store:
        gateway = setup(store)
        source = gateway.perception.connect("pc")
        for n in range(200):
            await source.observe(
                "document", f"meeting-{n}.md", occurred_at=BASE + timedelta(hours=n)
            )

        def should_not_open(*args, **kwargs):
            pytest.fail("Whole backlog must render from incremental caches")

        monkeypatch.setattr(ActivityItem, "model_validate_json", should_not_open)
        message = store.pending_timeline("work", "u")
        assert (
            message and estimate(message.model_text()) <= 800 and "meeting-199.md" in message.text
        )
