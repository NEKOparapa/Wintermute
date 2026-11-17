"""Activity summaries remain separate from each conversation's delivery acknowledgements."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wintermute.agent import AssistantMessage
from wintermute.events import ActivityItem, EventGateway, SliceStreamItem, SQLiteStore, Terminal
from wintermute.events.core.activity import perception_activity, timeline_line
from wintermute.events.core.perception_tokens import count_tokens
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
        await device.observe("document", "EARLIER" + " x" * 209, occurred_at=BASE)
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

        short = await visit("误切", -20, -17)
        assert store.pending_timeline("work", "u") is None
        assert store.get_event(short.event.event_id).payload["value"]["foreground_app"] == "误切"
        # A 6.5 s visit consisting of two brief titles contributes no context.
        empty = await visit(
            "Chrome",
            -10,
            -3.5,
            work_objects=[
                {"occurred_at": at(-10), "window_title": "A"},
                {"occurred_at": at(-7), "window_title": "B"},
            ],
        )
        detail = store.perception_stages("u", "work", empty.event.event_id, ai_enabled=True)
        assert detail["retained"] is None and detail["parts"][0]["text"] == ""
        assert detail["filter_reason"] == "short_dwell"
        assert [r["seconds"] for r in detail["decisions"] if r["kind"] == "title"] == [3, 3.5]
        assert store.pending_timeline("work", "u") is None and store.next_timeline_batch() is None
        assert not store.list_states(user_id="u", conversation_id="work")
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
        assert msg and msg.text.count("Editor ") == 2
        assert msg.text.index("会议纪要") < msg.text.index("合同")
        assert "会议已保存" in msg.text and "结束闪现" not in msg.text and "启动页" not in msg.text
        assert "误切" not in msg.text and "10.0 秒\n内容：" in msg.text
        assert store.get_event(event.event.event_id).payload["value"]["window_title"] == "结束闪现"

        for offset, total, removed, expected in [
            (40, 6.514, 1.478, 5.036),
            (50, 7, 2, 5),
            (60, 7, 2.001, None),
        ]:
            objects = [
                {
                    "occurred_at": at(offset),
                    "window_title": "短停留",
                    "ended_at": at(offset + removed),
                }
            ]
            if expected == 5.036:
                objects.append({"occurred_at": at(offset + removed), "window_title": "Toy 广场"})
            receipt = await visit(
                "Chrome",
                offset,
                offset + total,
                work_objects=objects,
            )
            detail = store.perception_stages("u", "work", receipt.event.event_id, ai_enabled=True)
            if expected is None:
                assert detail["retained"] is None and detail["parts"][0]["text"] == ""
            else:
                assert detail["retained"]["value"]["duration_seconds"] == expected


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


async def test_fixed_app_filter_retains_raw_history_and_matches_identity_not_titles(tmp_path):
    path = tmp_path / "app-filter.db"
    with SQLiteStore(path) as store:
        sensor = setup(store).perception.connect("pc")
        for state, value in [
            (
                "desktop.window_span",
                {"foreground_app": " WINTERMUTE.exe ", "duration_seconds": 180},
            ),
            ("desktop.window_span", {"foreground_app": "Wintermute.app"}),
            ("mobile.app_activity", "前台 · 聊天"),
        ]:
            event = (await sensor.observe(state, value)).event
            assert store.get_event(event.event_id).payload["value"] == value
            assert store.pending_timeline("work", "u") is None
            assert store.next_timeline_batch() is None
            assert not store.list_states(user_id="u", conversation_id="work")
            detail = store.perception_stages("u", "work", event.event_id, ai_enabled=True)
            assert detail["filter_reason"] == "wintermute_app"
            assert not detail["parts"] and not detail["rounds"] and not detail["context_messages"]
        other = await sensor.observe(
            "desktop.window_span",
            {"foreground_app": "Editor", "window_title": "Wintermute", "duration_seconds": 20},
        )
        assert "Wintermute" in store.pending_timeline("work", "u").text
    with SQLiteStore(path) as store:
        rows, _ = store.browse_history(
            user_id="u", conversation_id="work", view="perceptiontimeline"
        )
        assert len(rows) == 4
        for row in rows:
            if row.id == other.event.event_id:
                assert row.filter_result == "kept"
            else:
                assert row.filter_result == "filtered" and row.usage["tokens"] == 0


async def test_rolling_input_resumes_and_discards_only_delivered_generation(tmp_path: Path):
    path = tmp_path / "rolling.db"
    policy = TimelinePolicy(delivery_tokens=256, summary_tokens=80)
    with SQLiteStore(path, clock=lambda: BASE, timeline_policy=policy) as store:
        sensor = setup(store).perception.connect("pc")
        await sensor.observe("page", "FIRST" + " a" * 250)
        first = store.next_timeline_batch()
        assert first and "FIRST" in first.rule_text
        await sensor.observe("page", "SECOND" + " b" * 260)
    with SQLiteStore(path, clock=lambda: BASE, timeline_policy=policy) as store:
        assert store.next_timeline_batch() == first
        store.finish_timeline_batch(first.batch_id, "第一轮摘要")
        second = store.next_timeline_batch()
        assert second and "第一轮摘要" in second.rule_text and "SECOND" in second.rule_text
        parent = store.trace_members("u", "work", f"round-{second.batch_id}", None, 10)["round"][
            "previous_summary"
        ]
        assert parent["id"] == f"round-{first.batch_id}" and parent["usage"]["devices"] == ["pc"]
        assert "FIRST" not in second.rule_text and " a" * 250 not in second.rule_text
        store.finish_timeline_batch(second.batch_id, "第二轮摘要")
        assert store.next_timeline_batch() is None
        sensor = EventGateway(store).perception.connect("pc")
        await sensor.observe("page", "THIRD" + " c" * 260)
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


@pytest.mark.parametrize("new_tokens", [799, 800, 801])
async def test_perception_counts_only_new_record_tokens_at_threshold(new_tokens):
    with SQLiteStore(clock=lambda: BASE) as store:
        sensor = setup(store).perception.connect("pc")
        first = await sensor.observe("page", " x" * 805, occurred_at=BASE)
        prefix_cost = count_tokens(timeline_line(perception_activity(first.event))) - 805
        parent = store.next_timeline_batch()
        original = "上一轮完整摘要。" * 1000 + "结尾"
        store.finish_timeline_batch(parent.batch_id, original)
        assert store.next_timeline_batch() is None
        record = await sensor.observe("page", " x" * (new_tokens - prefix_cost), occurred_at=BASE)
        detail = store.perception_stages("u", "work", record.event.event_id, ai_enabled=True)
        assert detail["parts"][0]["tokens"] == detail["pending_size"] == new_tokens
        batch = store.next_timeline_batch()
        if new_tokens < 800:
            assert batch is None
        else:
            assert (
                batch.rule_text
                == f"已有摘要：\n{original}\n新增观测（按发生时间）：\n"
                + detail["parts"][0]["text"]
            )


async def test_upgrade_recounts_unread_lines_and_rechecks_unfinished_byte_round(tmp_path):
    path = tmp_path / "byte-round.db"
    with SQLiteStore(
        path, clock=lambda: BASE, timeline_policy=TimelinePolicy(delivery_tokens=128)
    ) as store:
        sensor = setup(store).perception.connect("pc")
        src = await sensor.observe("page", " new" * 130)
        old_round = store.next_timeline_batch()
        assert old_round
    with sqlite3.connect(path) as old:
        old.execute("ALTER TABLE timeline_batch_members DROP COLUMN perception_tokens")
        old.execute("UPDATE perception_buffers SET tail_cost=888")
        old.execute("PRAGMA user_version=26")
    with SQLiteStore(path, clock=lambda: BASE) as store:
        assert store.next_timeline_batch() is None
        trace = store.perception_stages("u", "work", src.event.event_id, ai_enabled=True)
        assert trace["pending_size"] == count_tokens(trace["parts"][0]["text"]) < 800
        assert not trace["rounds"]
        await EventGateway(store).perception.connect("pc").observe("page", " more" * 700)
        assert store.next_timeline_batch()


@pytest.mark.parametrize("state", ["unread", "generating", "summarized", "delivered"])
@pytest.mark.parametrize("version", [27, 28])
async def test_three_line_upgrade_reformats_sources_and_preserves_frozen_inputs(
    tmp_path, state, version
):
    path = tmp_path / "window-text.db"
    start, end = "2026-11-17T17:01:23.904Z", "2026-11-17T17:01:29.945Z"
    now = datetime.fromisoformat(end)
    policy = TimelinePolicy(delivery_tokens=128)
    old_line = (
        f"{start} 至 {end}，约 6.041 秒。应用「ShellHost」。\n"
        "    工作对象：17:01:23 快速设置 | 来源设备：pc"
    )
    new_line = (
        f"ShellHost {start} 至 {end} 6.041 秒\n内容：17:01:23 快速设置\n设备：pc"
    )
    with SQLiteStore(path, clock=lambda: now, timeline_policy=policy) as store:
        gateway = setup(store)
        record = await gateway.perception.connect("pc").observe(
            "desktop.window_span",
            {
                "foreground_app": "ShellHost",
                "window_title": "快速设置",
                "started_at": start,
                "ended_at": end,
                "duration_seconds": 6.041,
                "work_objects": [{"occurred_at": start, "window_title": "快速设置"}],
            },
            occurred_at=now,
        )
        # Recreate v27's stored source before freezing or delivering it through normal callers.
        with store._transaction():
            store._connection.execute(
                "UPDATE timeline_batch_members SET perception_line=?,perception_tokens=?",
                (old_line, count_tokens(old_line)),
            )
            store._connection.execute("UPDATE timeline_batches SET rule_text=?", (old_line,))
            store._connection.execute(
                "UPDATE perception_buffers SET tail_cost=?", (count_tokens(old_line),)
            )
        frozen = None
        if state in ("generating", "summarized"):
            await gateway.perception.connect("pc").observe("page", " x" * 128, occurred_at=now)
            frozen = store.next_timeline_batch()
            assert frozen and frozen.rule_text.startswith(old_line + "\n")
            if state == "summarized":
                store.finish_timeline_batch(frozen.batch_id, "摘要原文")
        if state == "delivered":
            await read(gateway)
        history = store.history("work", "u")
        raw = store.get_event(record.event.event_id)
        pending = store.perception_stages("u", "work", record.event.event_id, ai_enabled=True)[
            "pending_size"
        ]
    with sqlite3.connect(path) as old:
        old.execute(f"PRAGMA user_version={version}")
    for _ in range(2):
        with SQLiteStore(path, clock=lambda: now, timeline_policy=policy) as store:
            detail = store.perception_stages("u", "work", record.event.event_id, ai_enabled=True)
            expected = new_line
            assert detail["parts"][0]["text"] == expected
            assert detail["parts"][0]["tokens"] == count_tokens(expected)
            assert store.get_event(record.event.event_id) == raw
            assert store.history("work", "u") == history
            if state in ("unread", "generating"):
                expected_pending = pending - count_tokens(old_line) + count_tokens(new_line)
                assert detail["pending_size"] == expected_pending
            if state == "generating":
                assert store.next_timeline_batch() == frozen
    if state == "generating":
        with SQLiteStore(path, clock=lambda: now, timeline_policy=policy) as store:
            sensor = EventGateway(store).perception.connect("pc")
            later = await sensor.observe("page", "arrived during summary", occurred_at=now)
            store.finish_timeline_batch(frozen.batch_id, "摘要原文")
            detail = store.perception_stages("u", "work", later.event.event_id, ai_enabled=True)
            assert detail["pending_size"] == detail["parts"][0]["tokens"]
            assert store.next_timeline_batch() is None


async def test_existing_round_references_and_device_durations_survive_v23_upgrade(tmp_path):
    path = tmp_path / "references.db"
    policy = TimelinePolicy(delivery_tokens=128)
    with SQLiteStore(path, timeline_policy=policy) as store:
        gateway = setup(store)
        gateway.register_terminal(Terminal(terminal_id="laptop", user_id="u"))
        rounds = []
        for device, seconds in [("pc", 8), ("laptop", 13)]:
            await gateway.perception.connect(device, conversation_id="work").observe(
                "desktop.window_span",
                {"foreground_app": "Editor" * 145, "duration_seconds": seconds},
            )
            batch = store.next_timeline_batch()
            assert batch
            rounds.append(f"round-{batch.batch_id}")
            store.finish_timeline_batch(batch.batch_id, "相同的摘要")
    with sqlite3.connect(path) as old:
        for column in ("previous_batch_id", "devices", "duration_seconds"):
            old.execute(f"ALTER TABLE perception_rounds DROP COLUMN {column}")
        old.execute("PRAGMA user_version=23")
    with SQLiteStore(path, timeline_policy=policy) as store:
        rows, _ = store.browse_history(
            user_id="u", conversation_id="work", view="perceptiontimeline"
        )
        refs = {r.id: r.summary_round for r in rows if r.summary_round}
        assert refs[rounds[1]]["usage"]["devices"] == ["laptop", "pc"]
        assert refs[rounds[1]]["usage"]["duration_seconds"] == 21
        assert refs[rounds[0]]["usage"]["duration_seconds"] == 8
        current = store.trace_members("u", "work", rounds[1], None, 1)
        assert current["round"]["previous_summary"] == refs[rounds[0]]
        assert current["items"][0]["usage"]["duration_seconds"] == 13
        assert (
            store.trace_members("u", "work", rounds[0], None, 1)["round"]["previous_summary"]
            is None
        )


async def test_smaller_budget_after_restart_waits_for_new_observations(tmp_path):
    path = tmp_path / "reconfigure.db"
    with SQLiteStore(path, timeline_policy=TimelinePolicy(delivery_tokens=800)) as store:
        sensor = setup(store).perception.connect("pc")
        await sensor.observe("page", " x" * 900)
        job = store.next_timeline_batch()
        store.finish_timeline_batch(job.batch_id, "先前的摘要" * 17)
        earlier_id = job.batch_id
    # The preceding schema saved the frozen input and output but no completion timestamp.
    with sqlite3.connect(path) as legacy:
        legacy.execute("DROP TABLE perception_summary_history")
        legacy.execute("PRAGMA user_version=22")
    with SQLiteStore(path, timeline_policy=TimelinePolicy(delivery_tokens=128)) as store:
        assert store.next_timeline_batch() is None
        await EventGateway(store).perception.connect("pc").observe("page", " new" * 130)
        job = store.next_timeline_batch()
        assert job and "先前的摘要" * 17 in job.rule_text
        store.finish_timeline_batch(job.batch_id, "摘要已缩短")
        store.finish_timeline_batch(job.batch_id, "重复完成")
        rows, _ = store.browse_history(
            user_id="u", conversation_id="work", view="perceptiontimeline"
        )
        summaries = {row.id: row for row in rows if row.summary_round}
        assert set(summaries) == {f"round-{earlier_id}", f"round-{job.batch_id}"}
        assert summaries[f"round-{earlier_id}"].summary_round["generated_at"] is None
        latest = summaries[f"round-{job.batch_id}"]
        assert (
            latest.text == "摘要已缩短"
            and latest.summary_round["generated_at"] == latest.occurred_at
        )
        details = store.trace_members("u", "work", latest.id, None, 10)
        assert len(details["items"]) == 1 and "先前的摘要" * 17 in details["round"]["input"]
        assert details["round"]["previous_summary"]["id"] == f"round-{earlier_id}"
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
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 29
        pending = store.pending_timeline("work", "u")
        assert pending and "待发" in pending.text and "已读" not in pending.text
        assert store.next_timeline_batch() is None
