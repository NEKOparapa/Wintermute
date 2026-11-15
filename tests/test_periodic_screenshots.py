"""Atomic screenshot grouping, delivery, scope boundaries and the v10 browsing projection."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wintermute.events import EventInput, SQLiteStore, Terminal
from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.types import EventError
from wintermute.events.slices.image import ScreenshotObservation
from wintermute.events.slices.processor import SliceProcessor

AT = datetime(2026, 11, 15, 2, tzinfo=UTC)
DAY = "2026-11-15"


def register(store):
    for term in ("laptop", "phone"):
        store.register_terminal(Terminal(terminal_id=term, user_id="me"))
    for room in ("main", "private"):
        store.register_conversation(room, "me")


def capture(
    store, name, minute=0, *, room="main", terminal="laptop", app="QQ", digest="a", span="s"
):
    return store.append(
        EventInput(
            producer_event_id=name,
            terminal_id=terminal,
            conversation_id=room,
            layer="slice",
            source_id="app_screenshot",
            type="slice.image",
            occurred_at=AT + timedelta(minutes=minute),
            payload={
                "sha256": digest * 64,
                "capture": {"platform": "windows", "app": app, "title": app, "activity_id": span},
            },
        )
    ).event


class Vision:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.references = []

    async def observe(self, event, previous):
        self.references.append(previous)
        return next(self.observations)


def observation(*facts, subject="好友", recognized=True, changed=False):
    return ScreenshotObservation(subject, facts, (), recognized, changed)


async def test_repeated_frame_is_complete_and_omitted_from_timeline_across_restart(tmp_path: Path):
    path = tmp_path / "frames.db"
    with SQLiteStore(path) as store:
        register(store)
        first = capture(store, "one")
        skipped = capture(store, "two", 1, digest="b")
        vision = Vision(
            [
                replace(observation("对方询问家庭组"), evidence_keys=("对方·想加入家庭组",)),
                observation(recognized=False),
            ]
        )
        assert await SliceProcessor(store, image_processor=vision).process_pending() == 2
        assert store.get_slice_status(skipped.event_id).status == "completed"
        assert store.get_slice_status(skipped.event_id).item_id is None
        assert not store.has_unprocessed_slices() and not store.pending_slices()
        assert [a.source_event_ids for a in store.list_activity(user_id="me")] == [
            (first.event_id,)
        ]
    with SQLiteStore(path) as store:
        new = capture(store, "three", 2, digest="c")
        vision = Vision([observation("用户表示已发送邀请")])
        assert await SliceProcessor(store, image_processor=vision).process_pending() == 1
        (prev,) = vision.references
        assert prev.digest == "a" * 64
        assert prev.facts == ("对方询问家庭组",)
        assert prev.evidence_keys == ("对方·想加入家庭组",)
        assert store.get_slice_status(new.event_id).status == "completed"
        rows = store.list_activity(user_id="me")
        assert len(rows) == 2 and "已发送邀请" in rows[-1].content["text"]
        assert "询问家庭组" not in rows[-1].content["text"]
        assert store._connection.execute(
            "SELECT frame_count,change_count FROM screenshot_groups"
        ).fetchone()[:] == (3, 2)


def commit(store, event, facts, *, changed=False):
    prior = store.preceding_screenshot(event, DAY)
    result = observation(*facts, changed=changed)
    store.commit_screenshot(event, prior, result, timezone="UTC", text="；".join(facts))


def test_context_boundaries_and_changed_reference_during_recognition():
    with SQLiteStore() as store:
        register(store)
        a = capture(store, "one")
        b = capture(store, "two", 1)
        assert store.preceding_screenshot(b, DAY) is None
        commit(store, a, ["已查看聊天"])
        with pytest.raises(EventError, match="reference changed"):
            store.commit_screenshot(b, None, observation(), timezone="UTC", text="")
        commit(store, b, [])
        commit(store, b, [])
        assert store.get_slice_status(b.event_id).attempts == 1
        for name, kw in (
            ("other-room", {"room": "private"}),
            ("global", {"room": None}),
            ("other-device", {"terminal": "phone"}),
            ("other-span", {"span": "next"}),
        ):
            other = capture(store, name, 2, **kw)
            assert store.preceding_screenshot(other, DAY) is None
            commit(store, other, ["新画面"])
        switch = capture(store, "app", 3, app="Chrome")
        assert store.preceding_screenshot(switch, DAY) is None
        # The interrupted Chrome frame is still present in the source stream.
        returned = capture(store, "returned", 4)
        assert store.preceding_screenshot(returned, DAY) is None
        commit(store, returned, ["回到聊天窗口"])
        late = capture(store, "late", 1)
        assert store.preceding_screenshot(late, DAY) is None
        commit(store, late, ["单独的迟到记录"])
        new_subject = capture(store, "new-subject", 5)
        commit(store, new_subject, ["另一个人的聊天窗口"], changed=True)
        assert (
            store._connection.execute(
                "SELECT group_id FROM screenshot_frames WHERE event_id=?", (new_subject.event_id,)
            ).fetchone()[0]
            == new_subject.event_id
        )


def old_item(event, text):
    return SliceStreamItem(
        item_id="slice_" + event.producer_event_id,
        user_id=event.user_id,
        conversation_id=event.conversation_id,
        terminal_id=event.terminal_id,
        source_id=event.source_id,
        day=DAY,
        timezone="UTC",
        started_at=event.occurred_at,
        ended_at=event.occurred_at,
        source_event_ids=(event.event_id,),
        text=text,
        strategy="image_description",
        created_at=AT,
    )


def test_v10_migration_groups_identical_neighbors_and_tracks_revisions(tmp_path: Path):
    path = tmp_path / "previous.db"
    with SQLiteStore(path) as store:
        register(store)
        a = capture(store, "one")
        b = capture(store, "two", 1)
        c = capture(store, "three", 2, digest="c")
        for source in (a, b, c):
            store.commit_slice(old_item(source, "旧的截图识别"))
        original = store.list_activity(user_id="me")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE screenshot_frames")
        db.execute("DROP TABLE screenshot_groups")
        db.execute("DROP INDEX slice_waiting")
        db.execute("ALTER TABLE slice_processing DROP COLUMN frame_done")
        db.execute("ALTER TABLE timeline_batches DROP COLUMN screenshot_fact_bytes")
        db.execute("PRAGMA user_version=10")
    with SQLiteStore(path) as store:
        assert not store.pending_slices()
        assert store.list_activity(user_id="me") == original
        rows = store._connection.execute(
            "SELECT frame_count,change_count FROM screenshot_groups ORDER BY rowid"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(2, 1), (1, 1)]
        revision = old_item(a, "更新后的截图识别").model_copy(update={"item_id": "revised"})
        store.commit_slice(revision)
        assert (
            store._connection.execute(
                "SELECT latest_fact FROM screenshot_groups WHERE group_id=?", (a.event_id,)
            ).fetchone()[0]
            == revision.text
        )


def test_periodic_ai_threshold_counts_only_new_fact_bytes(tmp_path: Path):
    path = tmp_path / "threshold.db"
    with SQLiteStore(path) as store:
        register(store)
        # Three facts together reach exactly (but do not exceed) the default 600B threshold.
        for i, char in enumerate(("a", "b", "c")):
            event = capture(store, str(i), i, digest=char)
            prior = store.preceding_screenshot(event, DAY)
            facts = observation(char * 200)
            store.commit_screenshot(
                event, prior, facts, timezone="UTC", text="很长的窗口元数据" * 30 + facts.facts[0]
            )
        commit(store, capture(store, "no-change", 3), [])
        assert store.next_timeline_batch() is None
        assert (
            store._connection.execute(
                "SELECT screenshot_fact_bytes FROM timeline_batches"
            ).fetchone()[0]
            == 600
        )
    with SQLiteStore(path) as store:
        commit(store, capture(store, "more", 4), ["好"])
        batch = store.next_timeline_batch()
        assert batch and "好" in batch.rule_text
        store.finish_timeline_batch(batch.batch_id, "三段文字后有新回应")
        assert store.next_timeline_batch() is None


def test_six_periods_of_verbose_screenshot_headers_cannot_trigger_ai():
    from wintermute.events.core.timeline_compression import TimelinePolicy

    now = [AT]
    with SQLiteStore(
        clock=lambda: now[0], timeline_policy=TimelinePolicy(ai_threshold=32)
    ) as store:
        register(store)
        for i in range(6):
            now[0] = AT + timedelta(minutes=20 * i)
            event = capture(store, str(i), i * 20)
            store.commit_screenshot(
                event,
                store.preceding_screenshot(event, DAY),
                observation(str(i)),
                timezone="UTC",
                text="很长的设备和应用标签" * 20 + str(i),
            )
        now[0] += timedelta(hours=1)
        assert store.next_timeline_batch() is None
        assert (
            store._connection.execute(
                "SELECT rule_tokens FROM timeline_rollups WHERE sealed=1"
            ).fetchone()[0]
            == 6
        )


async def test_mixed_batch_uses_existing_rule_threshold():
    from wintermute.events import EventGateway
    from wintermute.events.core.timeline_compression import TimelinePolicy

    policy = TimelinePolicy(raw_tokens=300, ai_threshold=200)
    with SQLiteStore(timeline_policy=policy, clock=lambda: AT) as store:
        register(store)
        event = capture(store, "one")
        store.commit_screenshot(
            event,
            None,
            observation("已打开文件"),
            timezone="UTC",
            text="较长的应用窗口标签" * 16 + "已打开文件",
        )
        assert store.next_timeline_batch() is None
        sensor = EventGateway(store).perception.connect("laptop", conversation_id="main")
        await sensor.observe("document", "项目会议", occurred_at=AT)
        job = store.next_timeline_batch()
        assert job and "项目会议" in job.rule_text and "已打开文件" in job.rule_text
