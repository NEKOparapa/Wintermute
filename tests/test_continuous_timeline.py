from datetime import UTC, datetime, timedelta

from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.slices.processor import SliceProcessor


async def test_merge_before_compression_revises_late_input_and_keeps_originals(tmp_path):
    path = tmp_path / "records.sqlite3"
    policy = TimelinePolicy(raw_tokens=120, ai_threshold=1, summary_tokens=80, delivery_tokens=800)
    start = datetime(2026, 11, 18, 10, tzinfo=UTC)
    scope = dict(user_id="owner", conversation_id="room")
    with SQLiteStore(path, timeline_policy=policy) as store:
        gateway = EventGateway(store)
        for device in ("desk", "recorder"):
            gateway.register_terminal(Terminal(terminal_id=device, user_id="owner"))
        gateway.register_conversation("room", "owner")
        gateway.register_conversation("other", "owner")
        perception = gateway.perception.connect("desk", conversation_id="room")
        slices = gateway.slice.connect("recorder", conversation_id="room")
        processor = SliceProcessor(store)
        await perception.observe("app", "草拟文件", occurred_at=start)
        body = "核对预算。\n" * 40
        receipt = await slices.record(body, occurred_at=start + timedelta(minutes=12))
        assert await processor.process_pending() == 1
        raw, _ = store.browse_history(**scope, view="slicetimeline")
        assert [(r.id, r.text) for r in raw] == [(receipt.event.event_id, body)]
        assert store.next_timeline_batch() is None
        await perception.observe("app", "最新明细", occurred_at=start + timedelta(minutes=20))
        job = store.next_timeline_batch()
        assert job is not None and job.rule_text.index("草拟文件") < job.rule_text.index("核对预算")
        assert "最新明细" not in job.rule_text
        assert "desk" in job.rule_text and "recorder" in job.rule_text
        await slices.record("迟到的材料", occurred_at=start + timedelta(minutes=5))
        assert await processor.process_pending() == 1
        store.finish_timeline_batch(job.batch_id, "过期摘要")
        replacement = store.next_timeline_batch()
        assert replacement is not None and replacement.batch_id != job.batch_id
        assert replacement.rule_text.index("迟到的材料") < replacement.rule_text.index("核对预算")
        store.finish_timeline_batch(replacement.batch_id, "合并后的资料摘要")
        first, cursor = store.browse_history(**scope, view="merged", limit=1)
        assert "最新明细" in first[0].text and cursor
        second, end = store.browse_history(**scope, view="merged", cursor=cursor)
        assert end is None and second[0].text == "合并后的资料摘要"
        assert [m["layer"] for m in second[0].members] == ["perception", "slice", "slice"]
        assert store.browse_history(user_id="owner", conversation_id="other", view="merged") == (
            [],
            None,
        )
        talk = gateway.conversation.connect("desk", "room")
        await talk.send("发生了什么？")
        store.acquire_scheduler("worker")
        run = store.claim_next(defer_input=True)
        assert run
        inputs = store.absorb_session_inputs(run.job_id, messages=())
        assert inputs[0].text.index("合并后的资料摘要") < inputs[0].text.index("最新明细")
        assert store.pending_timeline("room", "owner") is None
        await slices.record("更早的转写", occurred_at=start - timedelta(minutes=1))
        assert await processor.process_pending() == 1
        revision = store.pending_timeline("room", "owner")
        assert revision and "更新后的顺序" in revision.text and "更早的转写" in revision.text
    with SQLiteStore(path, timeline_policy=policy) as reopened:
        rows, _ = reopened.browse_history(**scope, view="merged")
        assert any(row.members for row in rows)
        assert len(reopened.browse_history(**scope, view="slicetimeline")[0]) == 3


async def test_recording_is_one_capture_even_after_late_transcription_and_migration(tmp_path):
    import sqlite3

    from wintermute.events.slices.audio import AudioSliceProcessor

    path = tmp_path / "late.sqlite3"
    when = datetime(2026, 11, 18, 9, tzinfo=UTC)
    scope = {"user_id": "owner", "conversation_id": "room"}
    with SQLiteStore(path) as store:
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="mobile", user_id="owner"))
        gateway.register_conversation("room", "owner")
        audio = gateway.slice.connect("mobile", conversation_id="room", source_id="recording:field")
        receipt = await audio.record_audio(
            {
                "file": "a" * 64 + ".ogg",
                "sha256": "a" * 64,
                "bytes": 20,
                "recording_id": "field",
                "sequence": 1,
                "start_sample": 0,
                "samples": 16000,
            },
            occurred_at=when,
        )
        raw = store.browse_history(**scope, view="slice")[0]
        assert len(raw) == 1 and raw[0].processing == "pending"
        event = receipt.event.event_id
        transcript = "核对预算后归档。\\n" * 200
        store.save_audio_transcript(event, transcript)
        processor = AudioSliceProcessor(store, None, storage_path=tmp_path)
        assert await SliceProcessor(store, audio_processor=processor).process_pending() == 1
        raw = store.browse_history(**scope, view="slice")[0]
        assert len(raw) == 1 and raw[0].processing == "ready" and raw[0].text == transcript
        assert raw[0].id == event and datetime.fromisoformat(raw[0].occurred_at) == when
        assert raw[0].media[0].event_id == event
    # A v32 installation has the same activity receipts and none of the merged projection.
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE merged_entries")
        db.execute("DROP TABLE merged_batches")
        db.execute("PRAGMA user_version=32")
    with SQLiteStore(path) as migrated:
        merged = migrated.browse_history(**scope, view="timeline")[0]
        assert len(merged) == 1 and merged[0].layer == "slice"
        assert "核对预算后归档" in merged[0].text
        assert migrated.browse_history(**scope, view="delivered")[0] == []
