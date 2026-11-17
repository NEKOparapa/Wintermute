"""Activity summaries remain separate from each conversation's delivery acknowledgements."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wintermute.agent import AssistantMessage
from wintermute.events import ActivityItem, EventGateway, SliceStreamItem, SQLiteStore, Terminal
from wintermute.events.core.activity import perception_activity, timeline_line
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


async def test_unread_budget_and_conversation_coverage_survive_restart(tmp_path: Path):
    now = BASE
    path = tmp_path / "coverage.db"
    policy = TimelinePolicy(delivery_tokens=240, ai_threshold=1, idle_seconds=60)
    with SQLiteStore(path, clock=lambda: now, timeline_policy=policy) as store:
        gateway = setup(store)
        device = gateway.perception.connect("pc")
        await device.observe("document", "EARLIER" + "x" * 145, occurred_at=BASE)
        assert store.next_timeline_batch() is None
        gateway.register_conversation("new", "u")
        await device.observe("document", "LATER", occurred_at=BASE)
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


async def test_local_day_boundary_does_not_trigger_perception_summary():
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
        assert task is None
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


async def test_short_visits_and_title_transitions_are_filtered_before_budgeting():
    def at(seconds):
        return (BASE + timedelta(seconds=seconds)).isoformat()

    with SQLiteStore(clock=lambda: BASE + timedelta(minutes=3)) as store:
        sensor = setup(store).perception.connect("pc")

        def visit(key, start, end, **extra):
            return sensor.observe(
                "desktop.window_span",
                {
                    "foreground_app": key,
                    "started_at": at(start),
                    "ended_at": at(end),
                    "duration_seconds": end - start,
                    **extra,
                },
                occurred_at=datetime.fromisoformat(at(end)),
            )

        short = await visit("误切", 0, 3)
        assert store.pending_timeline("work", "u") is None
        assert store.get_event(short.event.event_id).payload["value"]["foreground_app"] == "误切"
        event = await visit(
            "Editor",
            4,
            19,
            window_title="结束闪现",
            work_objects=[
                {"occurred_at": at(4), "window_title": "启动页"},
                {"occurred_at": at(7), "window_title": "会议纪要"},
                {"occurred_at": at(11), "window_title": "会议纪要"},
                {"occurred_at": at(17), "window_title": "结束闪现"},
            ],
            visual_observations=[
                {"occurred_at": at(18), "window_title": "结束闪现", "text": "会议已保存"}
            ],
        )
        await visit("Editor", 22, 28, window_title="合同")
        msg = store.pending_timeline("work", "u")
        assert msg and msg.text.count("应用「Editor」") == 2
        assert msg.text.index("会议纪要") < msg.text.index("合同")
        assert "会议已保存" in msg.text and "结束闪现" not in msg.text and "启动页" not in msg.text
        assert "误切" not in msg.text and "约 15 秒" in msg.text
        assert store.get_event(event.event.event_id).payload["value"]["window_title"] == "结束闪现"


async def test_upgrade_preserves_the_filter_explanation_for_four_second_visits(
    tmp_path, monkeypatch
):
    path = tmp_path / "dwell.sqlite"

    async def observe(sensor, offset):
        start = BASE + timedelta(seconds=offset)
        end = start + timedelta(seconds=4)
        receipt = await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "QQ",
                "started_at": start.isoformat(),
                "ended_at": end.isoformat(),
                "duration_seconds": 4,
            },
            occurred_at=end,
        )
        return receipt.event.event_id

    with monkeypatch.context() as old, SQLiteStore(path) as store:
        old.setattr("wintermute.events.storage.sqlite.SHORT_DWELL_SECONDS", 3)
        before = await observe(setup(store).perception.connect("pc"), 0)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE timeline_batch_members DROP COLUMN dwell_threshold")
        db.execute("PRAGMA user_version=20")
    with SQLiteStore(path) as store:
        after = await observe(EventGateway(store).perception.connect("pc"), 10)
        results, _ = store.browse_history(user_id="u", conversation_id="work", view="perception")
        assert {r.id: r.filter_result for r in results} == {before: "kept", after: "filtered"}
        for id_, threshold, kept in [(before, 3, True), (after, 5, False)]:
            trace = store.perception_stages("u", "work", id_, ai_enabled=False)
            assert trace["dwell_threshold_seconds"] == threshold
            assert trace["decisions"][0]["kept"] is kept
            assert bool(trace["parts"][0]["text"]) is kept


async def test_rolling_input_resumes_and_discards_only_delivered_generation(tmp_path: Path):
    path = tmp_path / "rolling.db"
    policy = TimelinePolicy(delivery_tokens=256, summary_tokens=80)
    with SQLiteStore(path, clock=lambda: BASE, timeline_policy=policy) as store:
        sensor = setup(store).perception.connect("pc")
        await sensor.observe("page", "FIRST" + "a" * 250)
        first = store.next_timeline_batch()
        assert first and "FIRST" in first.rule_text
        await sensor.observe("page", "SECOND" + "b" * 260)
    with SQLiteStore(path, clock=lambda: BASE, timeline_policy=policy) as store:
        assert store.next_timeline_batch() == first
        store.finish_timeline_batch(first.batch_id, "第一轮摘要")
        second = store.next_timeline_batch()
        assert second and "第一轮摘要" in second.rule_text and "SECOND" in second.rule_text
        assert "FIRST" not in second.rule_text and "a" * 250 not in second.rule_text
        store.finish_timeline_batch(second.batch_id, "第二轮摘要")
        assert store.next_timeline_batch() is None
        sensor = EventGateway(store).perception.connect("pc")
        await sensor.observe("page", "THIRD" + "c" * 260)
        third = store.next_timeline_batch()
        assert third and "第二轮摘要" in third.rule_text and "THIRD" in third.rule_text
        before = store.pending_timeline("work", "u")
        assert before and "第二轮摘要" in before.text and "THIRD" in before.text
        assert estimate(before.model_text()) <= 256
        await read(EventGateway(store))
        await sensor.observe("page", "下一次会议")
        store.finish_timeline_batch(third.batch_id, "过时摘要")
        current = store.pending_timeline("work", "u")
        assert current and "下一次会议" in current.text and "摘要" not in current.text
        assert store.next_timeline_batch() is None


async def test_perception_triggers_strictly_above_formatted_budget():
    policy = TimelinePolicy(delivery_tokens=128)
    with SQLiteStore(clock=lambda: BASE, timeline_policy=policy) as store:
        sensor = setup(store).perception.connect("pc")
        probe = await sensor.observe("page", "x", occurred_at=BASE)
        prefix_cost = estimate(timeline_line(perception_activity(probe.event))) - 1
        await read(EventGateway(store))
        await sensor.observe("page", "x" * (128 - prefix_cost), occurred_at=BASE)
        assert store.next_timeline_batch() is None
        await sensor.observe("page", "y", occurred_at=BASE)
        assert store.next_timeline_batch() is not None


async def test_smaller_budget_after_restart_recompresses_existing_checkpoint(tmp_path):
    path = tmp_path / "reconfigure.db"
    with SQLiteStore(path, timeline_policy=TimelinePolicy(delivery_tokens=800)) as store:
        sensor = setup(store).perception.connect("pc")
        await sensor.observe("page", "x" * 900)
        job = store.next_timeline_batch()
        store.finish_timeline_batch(job.batch_id, "先前的摘要" * 17)
    with SQLiteStore(path, timeline_policy=TimelinePolicy(delivery_tokens=128)) as store:
        job = store.next_timeline_batch()
        assert job and "先前的摘要" in job.rule_text
        store.finish_timeline_batch(job.batch_id, "摘要已缩短")
        assert store.next_timeline_batch() is None
        assert "摘要已缩短" in store.pending_timeline("work", "u").text


async def test_v19_unread_perception_is_reformatted_without_replaying_read_history(tmp_path):
    from wintermute.events.storage import timeline_batches

    path = tmp_path / "v19.db"
    with SQLiteStore(path, clock=lambda: BASE) as store:
        gateway = setup(store)
        sensor = gateway.perception.connect("pc")
        await sensor.observe("page", "已读", occurred_at=BASE)
        await read(gateway)
        transcript = store.history("work", "u")
        await sensor.observe("page", "待发", occurred_at=BASE + timedelta(minutes=1))
    # Recreate the old time-window cache and its schema over the same durable source/receipts.
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("DROP TABLE perception_rounds")
        db.execute("DROP TABLE perception_buffers")
        db.execute("DELETE FROM timeline_batch_members")
        db.execute("DELETE FROM timeline_batches")
        db.execute("ALTER TABLE timeline_batch_members DROP COLUMN perception_line")
        for seq, data in db.execute("SELECT seq,data FROM activity_items ORDER BY seq"):
            timeline_batches.append(
                db, seq, ActivityItem.model_validate_json(data), TimelinePolicy()
            )
        db.execute("PRAGMA user_version=19")
    with SQLiteStore(path) as store:
        assert store.history("work", "u") == transcript
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 21
        pending = store.pending_timeline("work", "u")
        assert pending and "待发" in pending.text and "已读" not in pending.text
        assert store.next_timeline_batch() is None
