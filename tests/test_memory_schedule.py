import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession
from test_chat_media import media_service

from wintermute.config.models import MemorySettings
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.core.types import Device, EventInput
from wintermute.events.runtime.memory_worker import MemoryWorker
from wintermute.events.storage.repositories.records import RecordBatchInput, RecordInput
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.memory import MemoryVault


def test_schedule_http_shared_by_account_and_persisted(tmp_path):
    async def check():
        route = "/v1/memory/schedule"

        def auth(device):
            return {"X-Device-ID": device, "Authorization": "Bearer " + device}

        root = tmp_path / "u" / "about-user"
        root.mkdir(parents=True)
        activity = root / "activity.md"
        activity.write_text("## 常用时间段\n- 统计时区：Asia/Shanghai", encoding="utf-8")
        custom = tmp_path / "v" / "about-user" / "activity.md"
        custom.parent.mkdir(parents=True)
        custom.write_text("# 自己的笔记", encoding="utf-8")
        async with (
            media_service(tmp_path, memory_users=("u", "v")) as (url, _, _),
            ClientSession(url) as client,
        ):
            assert not activity.exists()
            assert custom.read_text(encoding="utf-8") == "# 自己的笔记"
            assert (await client.get(route)).status == 401
            default = await (await client.get(route, headers=auth("esp32"))).json()
            assert (default["interval_days"], default["local_time"]) == (1, "03:00")
            assert default["timezone"] == "Asia/Shanghai"
            for days, clock in [(0, "03:00"), (True, "03:00"), (2, "3:00"), (2, "24:00")]:
                response = await client.post(
                    route,
                    headers=auth("desktop"),
                    json={"interval_days": days, "local_time": clock},
                )
                assert response.status == 400
            response = await client.post(
                route, headers=auth("desktop"), json={"interval_days": 3, "local_time": "01:15"}
            )
            assert response.status == 200
            saved = await response.json()
            assert saved["interval_days"] == 3 and saved["local_time"] == "01:15"
            assert saved["next_run_at"].endswith("T01:15:00+08:00")
            assert await (await client.get(route, headers=auth("mobile"))).json() == saved
            assert await (await client.get(route, headers=auth("esp32"))).json() == default
        async with (
            media_service(tmp_path, memory_users=("u",)) as (url, _, _),
            ClientSession(url) as client,
        ):
            assert await (await client.get(route, headers=auth("mobile"))).json() == saved
            assert (await client.get(route, headers=auth("esp32"))).status == 503
        assert not activity.exists()
        assert len(list((tmp_path / "u" / ".trash").iterdir())) == 1

    asyncio.run(check())


def test_due_run_processes_records_dialogue_and_maintenance(tmp_path):
    now = datetime.fromisoformat("2026-11-19T02:00:00+08:00")
    with SQLiteStore(tmp_path / "db", clock=lambda: now) as store:
        store.register_device(Device(device_id="desktop", user_id="u"))
        store.register_conversation("chat", "u")
        plan = store.memory.set_schedule("u", 3, "03:00")
        store.records.submit(
            "desktop",
            RecordBatchInput(
                record_type="document", items=[RecordInput(occurred_at=now, content="历史资料")]
            ),
        )
        vault = MemoryVault(tmp_path / "vault")
        vault.initialize()
        worker = MemoryWorker(store, {"u": vault}, Mock(), MemorySettings(), timezone=plan.zone.key)
        runner = AsyncMock()
        worker._agents["u"].run = runner

        async def check():
            nonlocal now
            now = datetime.fromisoformat("2026-11-19T02:59:59+08:00")
            assert not await worker.process_user("u")
            receipt = store.append(
                EventInput(
                    device_id="desktop",
                    conversation_id="chat",
                    producer_event_id="message",
                    layer="conversation",
                    type="user.message",
                    payload={"text": "对话内容"},
                    occurred_at=now,
                )
            )
            now = plan.next_run_at
            assert not await worker.process_user("u")
            runner.assert_not_awaited()
            store.sessions.acquire_scheduler("scheduler")
            store.sessions.fail_job(receipt.job_id, "test conversation ended", owner_id="scheduler")
            for _ in range(3):
                assert await worker.process_user("u", owner_id="scheduler")
            assert not await worker.process_user("u", owner_id="scheduler")
            assert [call.args[0] for call in runner.await_args_list] == [
                "record:document",
                "conversation",
                "maintenance",
            ]
            assert "历史资料" in runner.await_args_list[0].args[1]
            assert "对话内容" in runner.await_args_list[1].args[1]
            assert not store.records.pending("u")
            progress = store.memory.profile_status("u")
            assert progress.cursor == progress.latest_seq > 0
            assert store.memory.schedule("u").next_date == date(2026, 11, 22)
            assert not (vault.root / "about-user" / "activity.md").exists()

        asyncio.run(check())


def test_missed_runs_retry_once_and_preserve_original_cadence(tmp_path):
    now = datetime.fromisoformat("2026-11-01T08:00:00+08:00")
    path = tmp_path / "db"
    with SQLiteStore(path, clock=lambda: now) as store:
        assert store.memory.set_schedule("u", 3, "03:00").next_date == date(2026, 11, 2)
    now = datetime.fromisoformat("2026-11-12T08:00:00+08:00")
    with SQLiteStore(path, clock=lambda: now) as store:
        overdue = store.memory.set_schedule("u", 3, "03:00")
        assert overdue.next_date == date(2026, 11, 2)
        vault = MemoryVault(tmp_path / "vault")
        vault.initialize()
        worker = MemoryWorker(
            store, {"u": vault}, Mock(), MemorySettings(), timezone=overdue.zone.key
        )
        runner = AsyncMock(side_effect=[RuntimeError("offline"), None])
        worker._agents["u"].run = runner

        async def check():
            with pytest.raises(RuntimeError, match="offline"):
                await worker.process_user("u")
            assert store.memory.schedule("u") == overdue
            assert await worker.process_user("u")
            assert not await worker.process_user("u")
            assert store.memory.schedule("u").next_date == date(2026, 11, 14)
            assert runner.await_count == 2

        asyncio.run(check())
        updated = store.memory.set_schedule("u", 5, "08:30")
        store.memory.finish_schedule("u", overdue, None)
        assert store.memory.schedule("u") == updated


def test_idle_worker_wakes_at_the_scheduled_minute(tmp_path):
    async def check():
        loop = asyncio.get_running_loop()
        initial = datetime.fromisoformat("2026-11-19T02:59:59.800+08:00")
        started = loop.time()

        def clock():
            return initial + timedelta(seconds=loop.time() - started)

        with SQLiteStore(clock=clock) as store:
            vault = MemoryVault(tmp_path / "vault")
            vault.initialize()
            settings = MemorySettings(poll_interval=3600)
            worker = MemoryWorker(store, {"u": vault}, Mock(), settings, timezone="Asia/Shanghai")
            completed = asyncio.Event()

            async def on_run(channel, *args, **kwargs):
                assert channel == "maintenance" and clock() >= due.next_run_at
                completed.set()

            worker._agents["u"].run = AsyncMock(side_effect=on_run)
            started = loop.time()
            due = store.memory.schedule("u")
            store.sessions.acquire_scheduler("clock")
            task = asyncio.create_task(worker.run("clock"))
            try:
                await asyncio.wait_for(completed.wait(), timeout=5)
                assert store.memory.schedule("u").next_date == date(2026, 11, 20)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize(
    ("now", "clock", "first", "second"),
    [
        (
            "2026-03-07T12:00:00-05:00",
            "02:30",
            "2026-03-08T03:30:00-04:00",
            "2026-03-09T02:30:00-04:00",
        ),
        (
            "2026-10-31T12:00:00-04:00",
            "01:30",
            "2026-11-01T01:30:00-04:00",
            "2026-11-02T01:30:00-05:00",
        ),
    ],
)
def test_dst_runs_once_on_each_local_date(now, clock, first, second):
    current = datetime.fromisoformat(now)
    policy = TimelinePolicy(timezone="America/New_York")
    with SQLiteStore(clock=lambda: current, timeline_policy=policy) as store:
        plan = store.memory.set_schedule("u", 1, clock)
        assert plan.next_run_at.isoformat() == first
        current = datetime.fromisoformat(first)
        store.memory.finish_schedule("u", plan, None)
        assert store.memory.schedule("u").next_run_at.isoformat() == second
