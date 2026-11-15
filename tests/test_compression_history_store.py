"""Browsing stored compression is read-only and respects delivery visibility at both levels."""

from datetime import UTC, datetime, timedelta

import pytest

from wintermute.events import EventGateway, SliceStreamItem, SQLiteStore, Terminal
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.storage.compression_history import batch_detail, browse_batches

AT = datetime(2026, 11, 15, 1, tzinfo=UTC)


async def test_mixed_leaf_hides_cached_summary_when_only_one_member_is_visible():
    clock = [AT]
    policy = TimelinePolicy(raw_tokens=10_000, ai_threshold=1, idle_seconds=60)
    with SQLiteStore(clock=lambda: clock[0], timeline_policy=policy) as store:
        db = store._connection
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="pc", user_id="person"))
        gateway.register_conversation("early", "person")
        gateway.register_conversation("outsider", "other")
        private = await gateway.perception.connect("pc").observe(
            "file", "secret.txt", occurred_at=AT
        )
        gateway.register_conversation("late", "person")
        at = AT + timedelta(seconds=1)
        raw = await gateway.slice.connect("pc").record("source material", occurred_at=at)
        store.commit_slice(
            SliceStreamItem(
                item_id="extracted",
                user_id="person",
                terminal_id="pc",
                source_id="default",
                day="2026-11-15",
                timezone="UTC",
                started_at=at,
                ended_at=at,
                created_at=at,
                source_event_ids=(raw.event.event_id,),
                text="Visible subject.",
                strategy="rule",
            )
        )
        (full,) = browse_batches(db, "person", "early", "slice", None, 10, policy, True)["items"]
        assert (full["perception_count"], full["slice_count"], full["status"]) == (1, 1, "open")
        id_ = full["id"]
        clock[0] += timedelta(minutes=3)
        job = store.next_timeline_batch()
        assert job and str(job.batch_id) == id_
        assert (
            batch_detail(db, "person", "early", id_, None, 1, policy, True)["batch"]["status"]
            == "pending"
        )
        store.retry_timeline_batch(job.batch_id, delay_seconds=5)
        assert (
            batch_detail(db, "person", "early", id_, None, 1, policy, True)["batch"]["status"]
            == "retrying"
        )
        assert (
            batch_detail(db, "person", "early", id_, None, 1, policy, False)["batch"]["status"]
            == "disabled"
        )
        store.finish_timeline_batch(job.batch_id, "Secret and public summary")
        before = store.pending_timeline("early", "person")
        first = batch_detail(db, "person", "early", id_, None, 1, policy, True)
        assert (
            first["ai_text"] == "Secret and public summary" and "secret.txt" in first["rule_text"]
        )
        assert (
            first["items"][0]["id"] == private.event.event_id
            and first["batch"]["status"] == "ready"
        )
        last = batch_detail(db, "person", "early", id_, first["next_cursor"], 1, policy, True)
        assert last["items"][0]["id"] == "extracted" and last["items"][0]["source_count"] == 1
        assert last["next_cursor"] is None
        assert store.pending_timeline("early", "person") == before
        restricted = batch_detail(db, "person", "late", id_, None, 10, policy, True)
        assert restricted["batch"]["partial"] and restricted["batch"]["perception_count"] == 0
        assert restricted["batch"]["started_at"] != full["started_at"]
        assert [m["id"] for m in restricted["items"]] == ["extracted"]
        assert restricted["ai_text"] is restricted["rule_text"] is restricted["rollup"] is None
        assert not browse_batches(db, "person", "late", "perception", None, 10, policy, True)[
            "items"
        ]
        with pytest.raises(ValueError):
            batch_detail(db, "person", "late", id_, first["next_cursor"], 1, policy, True)
        with pytest.raises(LookupError):
            batch_detail(db, "other", "outsider", id_, None, 1, policy, True)


async def test_a_visible_leaf_does_not_expose_rollups_containing_hidden_siblings():
    policy = TimelinePolicy(raw_tokens=1, ai_threshold=1)
    with SQLiteStore(clock=lambda: AT, timeline_policy=policy) as store:
        db = store._connection
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="pc", user_id="person"))
        gateway.register_conversation("early", "person")
        sensor = gateway.perception.connect("pc")
        for index in range(6):
            if index == 1:
                gateway.register_conversation("late", "person")
            await sensor.observe("file", f"doc-{index}", occurred_at=AT + timedelta(seconds=index))
        parent = store.next_timeline_batch()
        assert parent and parent.batch_id < 0
        store.finish_timeline_batch(parent.batch_id, "AI summary of all six")
        first = browse_batches(db, "person", "early", "perception", None, 3, policy, True)
        id_ = first["items"][0]["id"]
        after = browse_batches(
            db, "person", "early", "perception", first["next_cursor"], 3, policy, True
        )
        assert (
            not after["next_cursor"]
            and len({b["id"] for b in first["items"] + after["items"]}) == 6
        )
        with pytest.raises(ValueError):
            browse_batches(db, "person", "early", "slice", first["next_cursor"], 3, policy, True)
        before = batch_detail(db, "person", "early", id_, None, 10, policy, True)
        assert (
            before["rollup"]["ai_text"] == "AI summary of all six"
            and "doc-0" in before["rollup"]["rule_text"]
        )
        restricted = batch_detail(db, "person", "late", id_, None, 10, policy, True)
        assert not restricted["batch"]["partial"] and "doc-5" in restricted["rule_text"]
        assert restricted["rollup"] is None
