import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from wintermute.agent import AgentContext, AgentRunner, AssistantMessage, ToolCall, ToolContext
from wintermute.agent.tools.conversation.replies import conversation_reply_tools
from wintermute.agent.tools.devices.messaging import device_messaging_tool
from wintermute.agent.tools.devices.parameters import DeviceMessagingRequest
from wintermute.config.models import HTTPDeviceBinding, HTTPSettings
from wintermute.events.core.types import event_message
from wintermute.events.ingress import EventGateway
from wintermute.events.runtime.scheduler import RunScheduler
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.interfaces.http import HTTPInterface
from wintermute.interfaces.http_devices import HTTPDevices

START = datetime(2026, 11, 23, 8, tzinfo=UTC)
SCOPE = {"user_id": "user", "conversation_id": "room"}
NOTIFY = {"action": "notify", "targets": ["phone"], "title": "出门", "body": "带把伞"}


def register(devices: HTTPDevices, device_id: str) -> None:
    devices.register(HTTPDeviceBinding(device_id=device_id, **SCOPE))


def book(store, devices, directory, call_id="initial", **fields):
    tool = device_messaging_tool(
        store,
        "user",
        "room",
        lambda: devices.notification_targets("user", "room"),
        directory,
        Mock(),
        lambda: "phone",
    )
    args = tool.parameters.model_validate(
        {**NOTIFY, "notify_at": (START + timedelta(hours=1)).isoformat(), **fields}, strict=True
    )
    result = asyncio.run(tool.execute(args, ToolContext(call_id, 1, "run")))
    assert not result.is_error, result.content
    return json.loads(result.content)


def inbox(store, device="phone"):
    return store.notifications.list_user_notifications(**SCOPE, device_id=device)[0]


def test_notify_requires_time_and_ask_keeps_its_own_arguments() -> None:
    for extra in (
        {},
        {"notify_at": None},
        {"notify_at": "2026-11-24T09:00:00"},
        {"notify_at": START.isoformat(), "expires_at": START.isoformat()},
    ):
        with pytest.raises(ValidationError):
            DeviceMessagingRequest.model_validate({**NOTIFY, **extra}, strict=True)
    ask = {"action": "ask", "title": "确认", "question": "几点出发？"}
    assert DeviceMessagingRequest.model_validate(ask, strict=True).action == "ask"
    with pytest.raises(ValidationError):
        DeviceMessagingRequest.model_validate({**ask, "notify_at": START}, strict=True)


def test_persisted_notification_fires_once_through_scheduler(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite"
    clock = START
    attachment = tmp_path / "route.txt"
    attachment.write_bytes(b"original route")
    with SQLiteStore(database, clock=lambda: clock) as store:
        devices = HTTPDevices(store, [], {})
        register(devices, "phone")
        receipt = book(store, devices, tmp_path, attachments=["route.txt"])
        assert receipt["status"] == "scheduled"
        entry = store.schedules.list_schedules(**SCOPE)[0][0]
        assert entry.id == receipt["schedule_id"] and entry.action == "notify_device"
        attachment.unlink()
        assert book(store, devices, tmp_path, attachments=["route.txt"]) == receipt
        assert not inbox(store) and not store.list_events("notification", **SCOPE)
        assert (
            store.schedules.dispatch_due_schedules(
                notification_targets=devices.notification_targets
            )
            == 0
        )
        store.schedules.update_schedule(
            **SCOPE,
            id=entry.id,
            title="新路线",
            content="走北门",
            trigger_at=START + timedelta(hours=2),
        )

    class Model:
        async def stream(self, context: AgentContext) -> AsyncIterator[AssistantMessage]:
            yield AssistantMessage(tool_calls=(ToolCall("end", "end_session", {}),))

    async def dispatch(store, devices):
        runner = AgentRunner(Model(), tools=conversation_reply_tools())
        scheduler = RunScheduler(EventGateway(store), lambda _: runner)
        scheduler.notification_targets = devices.notification_targets
        async with scheduler:
            await asyncio.wait_for(scheduler.wait_until_idle(), timeout=5)

    with SQLiteStore(database, clock=lambda: clock) as store:
        devices = HTTPDevices(store, [], {})
        clock = START + timedelta(hours=1)
        assert not store.schedules.has_due_schedules()
        clock += timedelta(hours=1)
        asyncio.run(dispatch(store, devices))
        (item,) = inbox(store)
        assert (
            item["id"] == receipt["id"] and item["title"] == "新路线" and item["body"] == "走北门"
        )
        assert item["received_at"] is None and datetime.fromisoformat(item["created_at"]) == clock
        file, rowid = store.notifications.notification_file(
            **SCOPE,
            device_id="phone",
            notification_id=item["id"],
            file_id=item["attachments"][0]["id"],
        )
        assert (
            store.notifications.read_notification_chunk(rowid, 0, file["size_bytes"])
            == b"original route"
        )
        (event,) = store.list_events("notification", **SCOPE)
        assert event.type == "device_notification.result" and event.correlation_id == entry.id
        assert event.payload["status"] == "sent" and event.payload["sent"] == ["phone"]
        assert "收件箱" in event_message(event).text
        assert store.schedules.get_schedule(**SCOPE, id=entry.id).status == "triggered"
        assert (
            store.schedules.dispatch_due_schedules(
                notification_targets=devices.notification_targets
            )
            == 0
        )
        assert book(store, devices, tmp_path, attachments=["route.txt"])["status"] == "sent"


def test_cancel_partial_failure_expiry_and_inbox_error(tmp_path: Path) -> None:
    clock = START
    with SQLiteStore(clock=lambda: clock) as store:
        devices = HTTPDevices(store, [], {})
        register(devices, "phone")
        register(devices, "desk")
        partial = book(store, devices, tmp_path, "both", targets=["phone", "desk"])
        cancelled = book(store, devices, tmp_path, "cancel")
        expires = START + timedelta(hours=2)
        expired = book(store, devices, tmp_path, "expire", expires_at=expires.isoformat())
        with pytest.raises(ValueError, match="过期"):
            store.schedules.update_schedule(**SCOPE, id=expired["schedule_id"], trigger_at=expires)
        store.schedules.delete_schedule(**SCOPE, id=cancelled["schedule_id"])
        assert book(store, devices, tmp_path, "cancel")["status"] == "cancelled"
        devices.archive("desk")
        clock = expires
        assert (
            store.schedules.dispatch_due_schedules(
                notification_targets=devices.notification_targets
            )
            == 2
        )
        assert [n["id"] for n in inbox(store)] == [partial["id"]]
        assert not inbox(store, "desk")
        results = {e.correlation_id: e.payload for e in store.list_events("notification", **SCOPE)}
        mixed = results[partial["schedule_id"]]
        assert (
            mixed["status"] == "partial"
            and mixed["failed"] == {"设备已解绑或不属于当前会话": ["desk"]}
            and mixed["sent"] == ["phone"]
        )
        assert results[expired["schedule_id"]]["status"] == "failed"
        assert results[expired["schedule_id"]]["failed"] == {"通知已过期": ["phone"]}

        (tmp_path / "file.txt").write_bytes(b"test")
        rejected = book(store, devices, tmp_path, "db-error", attachments=["file.txt"])
        store._connection.execute(
            "CREATE TRIGGER reject_file BEFORE INSERT ON notification_files "
            "BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
        store.schedules.create_schedule(**SCOPE, title="普通日程", trigger_at=clock)
        assert (
            store.schedules.dispatch_due_schedules(
                notification_targets=devices.notification_targets
            )
            == 2
        )
        assert [n["id"] for n in inbox(store)] == [partial["id"]]
        events = store.list_events("notification", **SCOPE)
        error = next(e for e in events if e.correlation_id == rejected["schedule_id"])
        assert error.payload["status"] == "failed"
        assert error.payload["failed"] == {"写入设备收件箱失败": ["phone"]}
        assert sum(e.type == "schedule.due" for e in events) == 1


def test_upgrade_preserves_existing_inbox_and_interrupted_dispatch_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "upgrade.sqlite"
    with SQLiteStore(database, clock=lambda: START) as store:
        register(HTTPDevices(store, [], {}), "phone")
        old_id = store.notifications.send_user_notification(
            **SCOPE,
            targets=("phone",),
            title="历史通知",
            body="正文",
            expires_at=None,
            files=[],
            request_key="old",
            fingerprint="old",
        )
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP TABLE scheduled_notification_files")
        db.execute("DROP TABLE scheduled_notifications")
        db.execute("PRAGMA user_version=38")

    with SQLiteStore(database, clock=lambda: START) as store:
        devices = HTTPDevices(store, [], {})
        assert [item["id"] for item in inbox(store)] == [old_id]
        plan = book(store, devices, tmp_path, notify_at=START)
        with monkeypatch.context() as patch:
            patch.setattr(store, "_append_event", Mock(side_effect=RuntimeError("interrupted")))
            with pytest.raises(RuntimeError, match="interrupted"):
                store.schedules.dispatch_due_schedules(
                    notification_targets=devices.notification_targets
                )
        assert [item["id"] for item in inbox(store)] == [old_id]
        assert not store.list_events("notification", **SCOPE)
    with SQLiteStore(database, clock=lambda: START) as store:
        devices = HTTPDevices(store, [], {})
        assert (
            store.schedules.dispatch_due_schedules(
                notification_targets=devices.notification_targets
            )
            == 1
        )
        assert [item["id"] for item in inbox(store)] == [plan["id"], old_id]
        assert len(store.list_events("notification", **SCOPE)) == 1


def test_calendar_groups_plans_and_deliveries_by_notification_time(tmp_path: Path) -> None:
    clock = datetime.fromisoformat("2026-11-23T16:00:00+08:00")
    database = tmp_path / "calendar.sqlite"
    with SQLiteStore(database, clock=lambda: clock) as store:
        api = HTTPInterface(HTTPSettings(enabled=True), EventGateway(store), base_dir=tmp_path)
        for device_id, name in [("phone", "我的手机"), ("desk", "书桌电脑")]:
            api.devices.register(
                HTTPDeviceBinding(device_id=device_id, display_name=name, **SCOPE),
                token=device_id + "-test",
            )
        booked = book(
            store,
            api.devices,
            tmp_path,
            notify_at="2026-11-23T23:58:00+08:00",
            targets=["phone", "desk"],
        )
        old = store.notifications.send_user_notification(
            **SCOPE,
            targets=("phone",),
            title="旧提醒",
            body="带上书",
            expires_at=None,
            files=[],
            request_key="old",
            fingerprint="old",
        )
        next_day = book(store, api.devices, tmp_path, "next", notify_at="2026-11-24T00:00:00+08:00")

        async def check():
            nonlocal clock
            async with TestClient(TestServer(api.create_app())) as client:

                async def calendar(day, device="phone", include=True):
                    start = datetime.fromisoformat(day + "T00:00:00+08:00")
                    params = {
                        "start": start.isoformat(),
                        "end": (start + timedelta(days=1)).isoformat(),
                        "limit": "1",
                    }
                    if include:
                        params["include_notifications"] = "1"
                    rows = []
                    while True:
                        response = await client.get(
                            "/v1/schedule",
                            params=params,
                            headers={
                                "X-Device-ID": device,
                                "Authorization": "Bearer " + device + "-test",
                            },
                        )
                        assert response.status == 200
                        page = await response.json()
                        rows.extend(page["items"])
                        if page["next_cursor"] is None:
                            return rows
                        params["cursor"] = page["next_cursor"]

                day = "2026-11-23"
                before = await calendar(day)
                assert [r["notification"]["id"] for r in before] == [old, booked["id"]]
                plan = before[-1]["notification"]
                assert plan["target_names"] == ["我的手机", "书桌电脑"]
                assert plan["status"] == "scheduled" and not plan["in_inbox"]
                assert (
                    len(await calendar(day, "desk")) == len(await calendar(day, include=False)) == 1
                )
                clock = datetime.fromisoformat("2026-11-24T00:10:00+08:00")
                store.schedules.dispatch_due_schedules(
                    notification_targets=api.devices.notification_targets
                )
                delivered = (await calendar(day))[-1]["notification"]
                assert delivered["id"] == booked["id"] and delivered["in_inbox"]
                assert delivered["notify_at"] == plan["notify_at"]
                assert datetime.fromisoformat(delivered["created_at"]) == clock
                assert [r["notification"]["id"] for r in await calendar("2026-11-24")] == [
                    next_day["id"]
                ]
                desk_notice = (await calendar("2026-11-24", "desk"))[0]["notification"]
                assert desk_notice["attachments"] == [] and not desk_notice["in_inbox"]
                store.schedules.delete_schedule(**SCOPE, id=booked["schedule_id"])
                assert [r["notification"]["id"] for r in await calendar(day)] == [old, booked["id"]]

        asyncio.run(check())

    # A v39 inbox had only the actual delivery time. Recover the planned time even
    # when the user has since removed the completed schedule from their calendar.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP INDEX notification_calendar")
        db.execute("DROP INDEX notification_target_devices")
        db.execute("ALTER TABLE user_notifications DROP COLUMN notify_at")
        db.execute("PRAGMA user_version=39")
    with SQLiteStore(database) as upgraded:
        notice = next(item for item in inbox(upgraded) if item["id"] == booked["id"])
        assert datetime.fromisoformat(notice["notify_at"]) == datetime.fromisoformat(
            "2026-11-23T23:58:00+08:00"
        )
