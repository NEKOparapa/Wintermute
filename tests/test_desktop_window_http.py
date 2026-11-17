"""A multi-frame desktop dwell is a single perception, including after retry or restart."""

import asyncio
import base64
import hashlib
import io
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


async def wait_recognition(store, producer, state):
    async with asyncio.timeout(20):
        while True:
            event = store.received_event("work", producer)
            if event and event.payload["image_recognition"] == state:
                return event
            await asyncio.sleep(0.02)


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
        yield AssistantMessage(f"观察{self.calls}")


async def test_saved_frames_resume_after_starting_without_a_model(tmp_path):
    cfg = AppConfig.model_validate(
        {
            "events": {
                "database_path": str(tmp_path / "db"),
                "slices": {"enabled": False, "image": {"storage_path": "shots"}},
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
                        }
                    ],
                }
            },
        }
    )
    first, end = "2026-11-17T09:00:10Z", "2026-11-17T09:01:10.001Z"
    plain = {
        "event_id": "visit",
        "occurred_at": end,
        "state_key": "desktop.window_span",
        "value": {
            "foreground_app": "Editor",
            "started_at": first,
            "ended_at": end,
            "duration_seconds": 60.001,
            "work_objects": [
                {"occurred_at": first, "window_title": "议程", "ended_at": "2026-11-17T09:00:18Z"},
                *[
                    {"occurred_at": f"2026-11-17T09:00:{n:02d}Z", "window_title": f"标签 {n}"}
                    for n in range(20, 40)
                ],
                {"occurred_at": "2026-11-17T09:01:08Z", "window_title": "短暂预览"},
            ],
        },
    }
    # Identical frames at different times each get their own raw description.
    body = {
        **plain,
        "frames": [
            {"occurred_at": t, "jpeg_base64": jpeg("blue"), "window_title": title}
            for t, title in ((first, "议程"), (end, "短暂预览"))
        ],
    }
    headers = {"X-Terminal-ID": "work", "Authorization": "Bearer test"}
    calls = 0
    second_described = asyncio.Event()

    class Model:
        async def stream(self, context):
            nonlocal calls
            calls += 1
            assert context.screenshot_url.startswith("data:image/jpeg;base64,")
            if calls == 1:
                raise RuntimeError("transient")
            yield AssistantMessage("风险清单")
            second_described.set()

    for launch in range(3):
        if launch == 1:
            settings = cfg.model_dump()
            settings["events"]["slices"]["image"].update(enabled=True, model={"model": "v"})
            cfg = AppConfig.model_validate(settings)
        with create_store(cfg.events) as store:
            async with (
                ConversationService(
                    cfg,
                    ModelSet(None, {}, image_model=Model() if launch else None),
                    store,
                    base_dir=tmp_path,
                ) as service,
                ClientSession(headers=headers) as client,
            ):
                host, port = service.http.addresses[0]
                base = f"http://{host}:{port}"
                if launch == 0:
                    own = {
                        **body,
                        "event_id": "self",
                        "value": {**body["value"], "foreground_app": "Wintermute"},
                    }
                    response = await client.post(base + "/v1/perceptions/desktop-window", json=own)
                    assert response.status == 202
                    saved = store.received_event("work", "self")
                    assert (
                        saved.payload["frames"] and saved.payload["image_recognition"] == "filtered"
                    )
                    assert (
                        store.next_desktop_frame() is None
                        and store.pending_timeline("room", "owner") is None
                    )
                    reply = await client.post(base + "/v1/perceptions/desktop-window", json=body)
                    assert reply.status == 202
                    assert (
                        store.received_event("work", "visit").payload["image_recognition"]
                        == "awaiting_model"
                    )
                    assert "39.0 秒" in store.pending_timeline("room", "owner").text
                elif launch == 1:
                    async with asyncio.timeout(10):
                        await second_described.wait()
                    event = store.received_event("work", "visit")
                    assert [
                        n["frame_index"] for n in event.payload["value"]["visual_observations"]
                    ] == [1]
                else:
                    event = await wait_recognition(store, "visit", "ready")
                    notes = event.payload["value"]["visual_observations"]
                    assert [(n["occurred_at"], n["text"]) for n in notes] == [
                        (first, "风险清单"),
                        (end, "风险清单"),
                    ]
                    assert calls == 3
                    timeline = store.pending_timeline("room", "owner")
                    assert (
                        timeline and "风险清单" in timeline.text and "短暂预览" not in timeline.text
                    )
                    assert notes[-1]["window_title"] == "短暂预览"
                    trace = await (
                        await client.get(base + f"/v1/history/perceptions/{event.event_id}/stages")
                    ).json()
                    projections = {part["kind"]: part["text"] for part in trace["parts"]}
                    assert "风险清单" in projections["visual"]
                    assert "短暂预览" not in projections["visual"]
                    assert "议程" in projections["observation"]
                    assert any(
                        row["label"] == "短暂预览" and not row["kept"] for row in trace["decisions"]
                    )
                    assert (await (await client.get(base + "/v1/history?view=slice")).json())[
                        "items"
                    ] == []
                    url = (
                        base
                        + "/v1/history/compression/sources/"
                        + event.event_id
                        + ":visual?activity=1"
                    )
                    source = await client.get(url)
                    assert (await source.json())["items"][0]["id"] == event.event_id


async def test_multiple_title_batches_commit_one_visit_with_all_six_downloads(tmp_path):
    cfg = AppConfig.model_validate(
        {
            "events": {
                "database_path": str(tmp_path / "db"),
                "slices": {
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
                        }
                    ],
                }
            },
        }
    )
    count = 0

    class Model:
        async def stream(self, context):
            nonlocal count
            count += 1
            yield AssistantMessage(f"观察{count}")

    def at(second):
        return f"2026-11-17T09:00:{second:02}.000Z"

    frames = [
        {
            "occurred_at": at(i),
            "window_title": "周会议程" if i <= 3 else "风险清单",
            "jpeg_base64": jpeg((i * 40, 23, 98)),
        }
        for i in range(1, 7)
    ]
    digest = hashlib.sha256()
    for f in frames:
        checksum = hashlib.sha256(base64.b64decode(f["jpeg_base64"])).hexdigest()
        digest.update(f"{f['occurred_at']}\0{f['window_title']}\0{checksum}\n".encode())
    body = {
        "event_id": str(uuid4()),
        "state_key": "desktop.window_span",
        "occurred_at": at(7),
        "value": {
            "foreground_app": "Editor",
            "started_at": at(0),
            "ended_at": at(7),
            "duration_seconds": 7,
        },
        "total_frames": 6,
        "manifest": digest.hexdigest(),
    }
    first = {**body, "offset": 0, "frames": frames[:3]}
    last = {**body, "offset": 3, "frames": frames[3:]}
    headers = {"X-Terminal-ID": "work", "Authorization": "Bearer test"}
    for restart in (False, True):
        with create_store(cfg.events) as store:
            async with (
                ConversationService(
                    cfg, ModelSet(None, {}, image_model=Model()), store, base_dir=tmp_path
                ) as service,
                ClientSession(headers=headers) as client,
            ):
                host, port = service.http.addresses[0]
                base = f"http://{host}:{port}"
                url = base + "/v1/perceptions/desktop-window/batches"
                assert (await client.post(url, json=last)).status == 503
                for _ in range(2):
                    r = await client.post(url, json=first)
                    assert r.status == 202 and await r.json() == {"next_offset": 3}
                assert store.received_event("work", body["event_id"]) is None
                assert count == 0
                if not restart:
                    continue
                bad = {**last, "frames": [*frames[3:5], {**frames[5], "jpeg_base64": jpeg("red")}]}
                assert (await client.post(url, json=bad)).status == 400
                assert store.received_event("work", body["event_id"]) is None
                result = await client.post(url, json=last)
                assert result.status == 202
                event_id = (await result.json())["event_id"]
                assert (await (await client.post(url, json=first)).json()) == {
                    "event_id": event_id,
                    "duplicate": True,
                }
                await wait_recognition(store, body["event_id"], "ready")
                assert count == 6
                history = await client.get(base + "/v1/history?view=perception")
                (item,) = (await history.json())["items"]
                assert len(item["files"]) == 6
                for file, frame in zip(item["files"], frames, strict=True):
                    assert file["window_title"] == frame["window_title"]
                    response = await client.get(base + "/v1/history/files/" + file["id"])
                    assert await response.read() == base64.b64decode(frame["jpeg_base64"])
                assert (
                    len(
                        store.received_event("work", body["event_id"]).payload["value"][
                            "visual_observations"
                        ]
                    )
                    == 6
                )


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
                    accepted = await client.post(path, json=request)
                    assert accepted.status == 202
                    async with asyncio.timeout(10):
                        await vision.paused.wait()
                    assert (await (await client.post(path, json=request)).json())["duplicate"]
                    pending_event = store.received_event("work", plain["event_id"])
                    assert pending_event.payload["image_recognition"] == "pending"
                    (first_note,) = pending_event.payload["value"]["visual_observations"]
                    assert first_note["text"] == "观察1"
                    assert len(list((tmp_path / "shots").rglob("*.jpg"))) == 2
                    page = await (await client.get(base + "/v1/history?view=perception")).json()
                    assert page["items"][0]["raw"]["event_id"] == pending_event.event_id
                    assert len(page["items"][0]["files"]) == 2
                    vision.release.set()
                    retrying = await wait_recognition(store, plain["event_id"], "retrying")
                    assert retrying.payload["value"]["visual_observations"] == [first_note]
                else:
                    assert not orphan.exists()
                    assert store.pending_frame_uploads() == []
                result = await client.post(path, json=request)
                assert result.status == 202, await result.text()
                reply = await result.json()
                assert reply["duplicate"] is True
                await wait_recognition(store, plain["event_id"], "ready")
                assert vision.calls == 3
                value = store.received_event("work", plain["event_id"]).payload["value"]
                assert value["work_objects"] == plain["value"]["work_objects"]
                assert value["visual_observations"] == [
                    {
                        "frame_index": 0,
                        "occurred_at": middle,
                        "window_title": "议程",
                        "text": "观察1",
                    },
                    {
                        "frame_index": 1,
                        "occurred_at": ended,
                        "window_title": "议程",
                        "text": "观察3",
                    },
                ]
                timeline = store.pending_timeline("room", "owner").text
                assert "观察1" in timeline and "观察3" in timeline
                assert "frame_index" not in timeline and "sha256" not in timeline
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
                assert legacy.status == (400 if slices_enabled else 503)
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
