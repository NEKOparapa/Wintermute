"""A multi-frame desktop dwell is a single perception, including after retry or restart."""

import asyncio
import base64
import io
import json
import sqlite3
from uuid import uuid4

import pytest
from aiohttp import ClientSession
from PIL import Image

from wintermute.agent import AssistantMessage
from wintermute.config import AppConfig
from wintermute.config.factory import ModelSet, create_store
from wintermute.interfaces.desktop_window import DIGEST_PREFIX
from wintermute.interfaces.media_file import desktop_frame_directory
from wintermute.interfaces.service import ConversationService


def jpeg(color):
    stream = io.BytesIO()
    Image.new("RGB", (140, 72), color).save(stream, "JPEG")
    return base64.b64encode(stream.getvalue()).decode()


def test_version_15_history_survives_frame_upload_journal_migration(tmp_path):
    from wintermute.events import SQLiteStore, Terminal

    database = tmp_path / "existing.db"
    with SQLiteStore(database) as store:
        store.register_terminal(Terminal(terminal_id="work", user_id="owner"))
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE desktop_frame_uploads")
        db.execute("PRAGMA user_version=15")
    with SQLiteStore(database) as store:
        assert store.get_terminal("work").user_id == "owner"
        assert store.pending_frame_uploads() == []


class Vision:
    def __init__(self):
        self.calls = 0
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, context):
        assert context.screenshot_url.startswith("data:image/jpeg;base64,")
        self.calls += 1
        if self.calls == 2:
            self.paused.set()
            await self.release.wait()
            raise RuntimeError("transient vision error")
        yield AssistantMessage(json.dumps({"subject": "议程", "facts": [f"观察{self.calls}"]}))


@pytest.mark.parametrize("slices_enabled", [True, False])
async def test_desktop_window_retry_privacy_and_revocation(tmp_path, slices_enabled):
    cfg = AppConfig.model_validate(
        {
            "events": {
                "database_path": str(tmp_path / "db"),
                "slices": {
                    "enabled": slices_enabled,
                    "image": {"enabled": True, "storage_path": "shots", "model": {"model": "v"}},
                },
            },
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {
                            "terminal_id": "work",
                            "user_id": "owner",
                            "conversation_id": "room",
                            "token": "test",
                        },
                        {
                            "terminal_id": "phone",
                            "user_id": "owner",
                            "conversation_id": "room",
                            "token": "phone",
                        },
                        {
                            "terminal_id": "other-room",
                            "user_id": "owner",
                            "conversation_id": "other",
                            "token": "other",
                        },
                        {
                            "terminal_id": "guest",
                            "user_id": "guest",
                            "conversation_id": "guest",
                            "token": "guest",
                        },
                    ],
                }
            },
        }
    )
    vision = Vision()
    models = ModelSet(None, {}, image_model=vision)
    headers = {"X-Terminal-ID": "work", "Authorization": "Bearer test"}
    started, middle, ended = ["2026-11-17T09:00:" + s + ".000Z" for s in ("00", "05", "10")]
    plain = {
        "event_id": str(uuid4()),
        "occurred_at": ended,
        "state_key": "desktop.window_span",
        "value": {
            "started_at": started,
            "ended_at": ended,
            "foreground_app": "Editor",
            "window_title": "议程",
            "duration_seconds": 10,
            "work_objects": [
                {"occurred_at": started, "window_title": "议程"},
            ],
        },
    }
    frames = [
        {"occurred_at": middle, "window_title": "议程", "jpeg_base64": jpeg("white")},
        {"occurred_at": ended, "window_title": "议程", "jpeg_base64": jpeg("blue")},
    ]
    request = {**plain, "frames": frames}
    # Simulate both sides of a power loss - after the first file write, or after
    # the event was committed but before the pending-upload entry was cleared.
    orphan_fingerprint = DIGEST_PREFIX + "f" * 64
    orphan = desktop_frame_directory(tmp_path / "shots", "owner", "work", orphan_fingerprint)

    for initial in (True, False):
        with create_store(cfg.events) as store:
            async with (
                ConversationService(cfg, models, store, base_dir=tmp_path) as service,
                ClientSession(headers=headers) as client,
            ):
                host, port = service.http.addresses[0]
                base = f"http://{host}:{port}"
                path = base + "/v1/perceptions/desktop-window"
                if initial:
                    broken = [{**frames[0], "jpeg_base64": base64.b64encode(b"bad").decode()}]
                    assert (await client.post(path, json={**plain, "frames": broken})).status == 400
                    pending = asyncio.create_task(client.post(path, json=request))
                    async with asyncio.timeout(10):
                        await vision.paused.wait()
                    assert (await client.post(path, json=request)).status == 429
                    assert store.received_event("work", plain["event_id"]) is None
                    assert not (tmp_path / "shots").exists()
                    vision.release.set()
                    assert (await pending).status == 503
                    assert not list((tmp_path / "shots").rglob("*.jpg"))
                else:
                    assert not orphan.exists()
                    assert store.pending_frame_uploads() == []
                result = await client.post(path, json=request)
                assert result.status == 202, await result.text()
                reply = await result.json()
                assert reply["duplicate"] is (not initial)
                assert vision.calls == 4
                value = store.received_event("work", plain["event_id"]).payload["value"]
                assert value["work_objects"] == plain["value"]["work_objects"]
                assert value["visual_observations"] == [
                    {"occurred_at": middle, "window_title": "议程", "text": "观察3"},
                    {"occurred_at": ended, "window_title": "议程", "text": "观察4"},
                ]
                assert "base64" not in str(value)
                assert len(list((tmp_path / "shots").rglob("*.jpg"))) == 2
                history = await client.get(base + "/v1/history?view=perception")
                (entry,) = (await history.json())["items"]
                assert entry["id"] == reply["event_id"]
                files = entry["files"]
                assert len(files) == 2
                source = await client.get(
                    base + "/v1/history/compression/sources/" + reply["event_id"]
                )
                assert (await source.json())["items"][0]["files"] == files
                assert not any(word in entry["text"] for word in ("sha256", ".jpg", ":frame:"))
                for file, frame in zip(files, frames, strict=True):
                    assert file["occurred_at"] == frame["occurred_at"]
                    assert file["window_title"] == "议程"
                    assert file["content_type"] == "image/jpeg"
                    url = base + "/v1/history/files/" + file["id"]
                    shared = await client.get(
                        url, headers={"X-Terminal-ID": "phone", "Authorization": "Bearer phone"}
                    )
                    assert shared.status == 200 and shared.content_type == "image/jpeg"
                    assert await shared.read() == base64.b64decode(frame["jpeg_base64"])
                    for terminal, token in (("other-room", "other"), ("guest", "guest")):
                        denied = await client.get(
                            url,
                            headers={"X-Terminal-ID": terminal, "Authorization": "Bearer " + token},
                        )
                        assert denied.status == 404
                history = await client.get(base + "/v1/history?view=slice")
                assert (await history.json())["items"] == []
                downgraded = await client.post(base + "/v1/perceptions", json=plain)
                assert await downgraded.json() == {"event_id": reply["event_id"], "duplicate": True}
                changed = [frames[0], {**frames[1], "jpeg_base64": jpeg("red")}]
                assert (await client.post(path, json={**plain, "frames": changed})).status == 409
                different = {**plain, "value": {**plain["value"], "duration_seconds": 8}}
                assert (await client.post(base + "/v1/perceptions", json=different)).status == 409
                legacy = await client.post(
                    base + "/v1/image/slices",
                    data=base64.b64decode(frames[0]["jpeg_base64"]),
                    headers={
                        "Content-Type": "image/jpeg",
                        "X-Event-ID": str(uuid4()),
                        "X-Occurred-At": middle,
                        "X-Capture-Context": base64.urlsafe_b64encode(
                            b'{"platform":"windows","app":"Editor"}'
                        ).decode(),
                    },
                )
                assert legacy.status == (422 if slices_enabled else 503)
                if not slices_enabled:
                    assert service.scheduler.slice_processor is None
                    manual = await client.post(
                        base + "/v1/image/slices",
                        data=base64.b64decode(frames[0]["jpeg_base64"]),
                        headers={
                            "Content-Type": "image/jpeg",
                            "X-Event-ID": str(uuid4()),
                            "X-Occurred-At": middle,
                        },
                    )
                    assert manual.status == 503
                if not initial:
                    # Rejection after writing JPEGs must preserve the committed visit's files.
                    store.max_event_bytes = 32
                    failed = {**request, "event_id": str(uuid4())}
                    assert (await client.post(path, json=failed)).status == 400
                    assert store.received_event("work", failed["event_id"]) is None
                    assert len(list((tmp_path / "shots").rglob("*.jpg"))) == 2
                else:
                    event = store.received_event("work", plain["event_id"])
                    assert store.begin_frame_upload(
                        "owner", "work", plain["event_id"], event.correlation_id
                    )
                    assert store.begin_frame_upload(
                        "owner", "work", "unfinished", orphan_fingerprint
                    )
                    orphan.mkdir()
                    (orphan / "partial.jpg").write_bytes(b"partial")
