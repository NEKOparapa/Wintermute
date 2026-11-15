"""Stored extraction provenance across media, revisions, suppressed input and sessions."""

from datetime import UTC, datetime

import pytest

from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.slices.image import ScreenshotObservation
from wintermute.events.storage.compression_sources import browse_sources

AT = datetime(2026, 11, 17, 3, tzinfo=UTC)


@pytest.fixture
def store():
    with SQLiteStore() as store:
        store.register_terminal(Terminal(terminal_id="pc", user_id="u"))
        store.register_conversation("a", "u")
        store.register_conversation("b", "u")
        yield store


def original_summary(events):
    return SliceStreamItem(
        item_id="old",
        user_id="u",
        conversation_id="a",
        terminal_id="pc",
        source_id="default",
        day="2026-11-17",
        timezone="UTC",
        started_at=AT,
        ended_at=AT,
        created_at=AT,
        source_event_ids=tuple(e.event.event_id for e in events),
        text="第一次识别的结果",
        strategy="test",
    )


async def test_media_transcript_sources_and_revised_group_keep_provenance(store):
    endpoint = EventGateway(store).slice.connect("pc", conversation_id="a")
    image = await endpoint.publish("slice.image", {"sha256": "a" * 64}, occurred_at=AT)
    audio = await endpoint.record_audio(
        {
            "sha256": "b" * 64,
            "file": "b" * 64 + ".ogg",
            "bytes": 10,
            "recording_id": "meeting",
            "sequence": 1,
            "start_sample": 0,
            "samples": 320,
        },
        occurred_at=AT,
    )
    aid = audio.event.event_id
    store.save_audio_transcript(aid, "其实原话包含了更多资料")
    prior = original_summary((image, audio))
    store.commit_slice(prior)
    db = store._connection
    page = browse_sources(db, "u", "a", "transcript-" + aid, limit=1)
    assert not db.in_transaction
    assert page.extraction["source_count"] == 2 and page.extraction["text"] == prior.text
    assert [row.event for row in page.items] == [audio.event]
    assert page.items[0].transcript == "其实原话包含了更多资料"
    assert page.next_cursor
    rest = browse_sources(db, "u", "a", aid, limit=1, cursor=page.next_cursor)
    assert not rest.next_cursor and rest.items[0].event == image.event

    revised = prior.model_copy(update={"item_id": "new", "text": "修订后的结果"})
    store.commit_slice(revised)
    assert browse_sources(db, "u", "a", "old", activity=True).extraction["text"] == prior.text
    assert browse_sources(db, "u", "a", aid).extraction["text"] == revised.text
    pinned = browse_sources(db, "u", "a", aid, cursor=page.next_cursor)
    assert pinned.items[0].event == image.event and pinned.extraction["text"] == prior.text
    with pytest.raises(ValueError):
        browse_sources(db, "u", "a", image.event.event_id, cursor=page.next_cursor)
    for identifier in (prior.item_id, revised.item_id, aid, "transcript-" + aid):
        with pytest.raises(LookupError):
            browse_sources(db, "u", "b", identifier)


async def test_no_change_shot_exposes_raw_capture_and_processing_state(store):
    endpoint = EventGateway(store).slice.connect("pc", conversation_id="a")
    payload = {"capture": {"platform": "windows", "app": "QQ"}, "sha256": "a" * 64}
    first = (await endpoint.publish("slice.image", payload, occurred_at=AT)).event
    db = store._connection
    assert browse_sources(db, "u", "a", first.event_id).extraction["status"] == "pending"
    store.mark_slice_failed([first.event_id], "TimeoutError", retry_delay=60)
    assert browse_sources(db, "u", "a", first.event_id).extraction["status"] == "retrying"
    store.commit_screenshot(
        first,
        None,
        ScreenshotObservation("好友", ("可以邀请",), (), True, False),
        timezone="UTC",
        text="用户表示可以邀请。",
    )
    second = (await endpoint.publish("slice.image", payload, occurred_at=AT)).event
    ref = store.preceding_screenshot(second, "2026-11-17")
    store.commit_screenshot(
        second, ref, ScreenshotObservation("好友", (), (), False, False), timezone="UTC", text=""
    )
    skipped = browse_sources(db, "u", "a", second.event_id)
    assert skipped.extraction["status"] == "no_change" and skipped.extraction["text"] is None
    assert skipped.items[0].event.payload == payload
    with pytest.raises(LookupError):
        browse_sources(db, "u", "a", second.event_id, activity=True)


async def test_raw_journal_and_delivered_activity_have_different_visibility(store):
    sensor = EventGateway(store).perception.connect("pc")
    event = (await sensor.observe("document", {"title": "简报", "pages": 3}, occurred_at=AT)).event
    store.register_conversation("late", "u")
    db = store._connection
    assert browse_sources(db, "u", "late", event.event_id).items[0].event.payload == event.payload
    assert (
        browse_sources(db, "u", "a", event.event_id, activity=True).extraction["status"] == "ready"
    )
    with pytest.raises(LookupError):
        browse_sources(db, "u", "late", event.event_id, activity=True)
    with pytest.raises(LookupError):
        browse_sources(db, "someone_else", "late", event.event_id)
    db.execute(
        "INSERT INTO desktop_snapshot_receipts VALUES(?,?,?,?,?)",
        ("pc", "legacy", "a", event.event_id, "test"),
    )
    for mode in (False, True):
        with pytest.raises(LookupError):
            browse_sources(db, "u", "a", event.event_id, activity=mode)


async def test_later_page_ownership_checked_before_first_page_summary(store):
    endpoint = EventGateway(store).slice.connect("pc", conversation_id="a")
    one = await endpoint.record("原始第一条", occurred_at=AT)
    two = await endpoint.record("原始第二条", occurred_at=AT)
    summary = original_summary((one, two))
    store.commit_slice(summary)
    # A malformed import referencing a different conversation must never expose the summary.
    store._connection.execute(
        "UPDATE slice_events SET conversation_id='b' WHERE event_id=?", (two.event.event_id,)
    )
    with pytest.raises(LookupError):
        browse_sources(store._connection, "u", "a", summary.item_id, limit=1)
