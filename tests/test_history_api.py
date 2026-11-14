"""Timeline entries show the delivered text; media files belong to the original slices."""

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from wintermute.config.models import (
    AudioSliceSettings,
    HTTPSettings,
    HTTPTerminalSettings,
    ImageSliceSettings,
)
from wintermute.events import EventGateway, SQLiteStore
from wintermute.events.core.slices import SliceStreamItem
from wintermute.interfaces.http import HTTPInterface


@pytest.mark.parametrize("history_format", ["current", "saved_sources", "individual"])
async def test_timeline_keeps_text_and_slice_keeps_downloads(tmp_path: Path, history_format: str):
    db_path = tmp_path / "events.db"
    capture = datetime(2026, 11, 14, 9, tzinfo=UTC)
    summary = "共享屏幕展示了会议议程，录音中确认周五交付。"
    settings = HTTPSettings(
        enabled=True,
        terminals=[
            HTTPTerminalSettings(
                terminal_id="desktop",
                user_id="user",
                conversation_id="work",
                token=SecretStr("test"),
            )
        ],
    )
    headers = {"X-Terminal-ID": "desktop", "Authorization": "Bearer test"}
    with SQLiteStore(db_path) as store:
        gateway = EventGateway(store)
        interface = HTTPInterface(
            settings,
            gateway,
            base_dir=tmp_path,
            image_settings=ImageSliceSettings(storage_path="images"),
            audio_settings=AudioSliceSettings(storage_path="audio"),
        )
        try:
            endpoint = gateway.slice.connect("desktop", conversation_id="work")
            image_bytes, audio_bytes = b"jpeg fixture", b"ogg fixture"
            image, audio = (sha256(b).hexdigest() for b in (image_bytes, audio_bytes))
            for directory, name, data in (
                ("images", image + ".jpg", image_bytes),
                ("audio", audio + ".ogg", audio_bytes),
            ):
                path = tmp_path / directory / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(data)
            photo = await endpoint.publish("slice.image", {"sha256": image}, occurred_at=capture)
            recording = await endpoint.record_audio(
                {
                    "file": audio + ".ogg",
                    "sha256": audio,
                    "bytes": len(audio_bytes),
                    "recording_id": "meeting",
                    "sequence": 1,
                    "start_sample": 0,
                    "samples": 320,
                },
                occurred_at=capture,
            )
            ids = (photo.event.event_id, recording.event.event_id)
            store.commit_slice(
                SliceStreamItem(
                    item_id="summary",
                    text=summary,
                    source_event_ids=ids,
                    user_id="user",
                    conversation_id="work",
                    terminal_id="desktop",
                    source_id="default",
                    day=capture.date().isoformat(),
                    timezone="UTC",
                    started_at=capture,
                    ended_at=capture,
                    created_at=capture,
                    strategy="meeting",
                )
            )
            if history_format == "current":
                await gateway.perception.connect("desktop", conversation_id="work").observe(
                    "desktop.window_span",
                    {
                        "foreground_app": "会议应用",
                        "window_title": "项目周会",
                        "started_at": "2026-11-14T08:59:30Z",
                        "ended_at": "2026-11-14T09:00:00Z",
                        "duration_seconds": 30,
                    },
                    occurred_at=capture,
                )
            await gateway.conversation.connect("desktop", "work").send("会议有什么结论？")
            store.acquire_scheduler("runner")
            if history_format == "individual":
                # Older sessions represent one activity per history row.
                with sqlite3.connect(db_path) as previous:
                    previous.execute(
                        "INSERT INTO session_history(conversation_id,activity_seq) "
                        "SELECT 'work',seq FROM activity_items"
                    )
                assert store.claim_next() is not None
            else:
                job = store.claim_next(defer_input=True)
                assert job is not None
                store.absorb_session_inputs(job.job_id, messages=())
                if history_format == "saved_sources":
                    # Existing reference lists must not turn old summaries back into attachments.
                    with sqlite3.connect(db_path) as previous:
                        previous.execute(
                            "CREATE TABLE IF NOT EXISTS history_timeline_sources "
                            "(run_id TEXT, input_index INTEGER, event_ids TEXT)"
                        )
                        previous.execute(
                            "INSERT OR REPLACE INTO history_timeline_sources "
                            "VALUES(?,0,json_array(?,?))",
                            (job.job_id, *ids),
                        )

            async with TestClient(TestServer(interface.create_app()), headers=headers) as client:

                async def items(view: str):
                    response = await client.get("/v1/history", params={"view": view})
                    assert response.status == 200
                    return (await response.json())["items"]

                timeline = await items("timeline")
                assert len(timeline) == 1
                assert summary in timeline[0]["text"] and timeline[0]["files"] == []
                if history_format == "current":
                    (span,) = await items("perception")
                    assert "约 30 秒。应用「会议应用」，窗口标题「项目周会」。" in span["text"]
                    assert span["text"] in timeline[0]["text"]
                    assert "感知 · desktop" in timeline[0]["text"]
                    assert span["files"] == []
                chat = await items("chat")
                assert timeline[0] in chat
                assert any(entry["text"] == "会议有什么结论？" for entry in chat)

                slices = await items("slice")
                assert all("桌面前台停留" not in row["text"] for row in slices)
                summary_row = next(entry for entry in slices if entry["id"] == "summary")
                assert {file["id"] for file in summary_row["files"]} == set(ids)
                for id_, expected, suffix in zip(
                    ids, (image_bytes, audio_bytes), (".jpg", ".ogg"), strict=True
                ):
                    original = next(entry for entry in slices if entry["id"] == id_)
                    (file,) = original["files"]
                    assert file["name"].endswith(suffix)
                    response = await client.get("/v1/history/files/" + file["id"])
                    assert response.status == 200 and await response.read() == expected
        finally:
            await interface.aclose()
