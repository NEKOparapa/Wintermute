import asyncio
import json
from datetime import UTC, datetime, timedelta
from time import time
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientSession
from test_chat_media import media_service

from wintermute.agent import ToolContext
from wintermute.agent.tools.calendar import holidays
from wintermute.agent.tools.calendar.calendar_query import CalendarQuery, calendar_tool
from wintermute.agent.tools.calendar.holidays import HolidayCalendar
from wintermute.agent.tools.calendar.time_query import TimeQuery, time_tool
from wintermute.events.storage.sqlite import SQLiteStore


def bulletin(year, rows):
    return json.dumps(
        {
            "year": year,
            "papers": [f"https://example.org/{year}"],
            "days": [{"date": day, "name": name, "isOffDay": off} for day, name, off in rows],
        }
    ).encode()


def test_local_time_and_dst():
    now = datetime(2026, 11, 19, 17, tzinfo=UTC)
    tool = time_tool(lambda: now, ZoneInfo("Asia/Shanghai"))

    def call(**args):
        return asyncio.run(tool.execute(TimeQuery(**args), ToolContext("time", 1)))

    assert json.loads(call().content)["datetime"] == "2026-11-20T01:00:00+08:00"
    for at, expected in [
        ("2026-07-01T09:00", "2026-06-30T21:00:00-04:00"),
        ("2026-11-20T09:00", "2026-11-19T20:00:00-05:00"),
    ]:
        result = call(
            action="convert", at=at, from_timezone="Asia/Shanghai", timezone="America/New_York"
        )
        assert json.loads(result.content)["datetime"] == expected
    for at in ("2026-03-08T02:30", "2026-11-01T01:30"):
        assert call(action="convert", at=at, from_timezone="America/New_York").is_error
    assert (
        json.loads(call(action="convert", at="2026-11-01T01:30-05:00", timezone="UTC").content)[
            "datetime"
        ]
        == "2026-11-01T06:30:00+00:00"
    )
    assert call(timezone="Missing/Zone").is_error


def test_annual_cache_and_cross_year_workdays(tmp_path):
    path = tmp_path / "test.db"
    documents = {
        2023: bulletin(2023, [("2023-01-01", "元旦", True)]),
        2024: bulletin(
            2024,
            [
                ("2023-12-30", "元旦", True),
                ("2023-12-31", "元旦", True),
                ("2024-01-01", "元旦", True),
            ],
        ),
    }
    calls = []

    async def fetch(year):
        calls.append(year)
        await asyncio.sleep(0)
        return documents.get(year)

    async def run():
        with SQLiteStore(path) as store:
            calendar = HolidayCalendar(store.holiday_years, fetch)
            tool = calendar_tool(store.now_utc, calendar)

            async def query(**args):
                response = await tool.execute(CalendarQuery(**args), ToolContext("calendar", 1))
                return json.loads(response.content), response.is_error

            month, _ = await asyncio.gather(
                query(action="month", month="2023-12"), query(action="day", date="2023-12-31")
            )
            assert calls == [2023, 2024]
            assert len(month[0]["days"]) == 31 and month[0]["days"][-1]["kind"] == "holiday"
            december, failed = await query(action="day", date="2024-12-02")
            assert (
                not failed
                and december["is_workday"]
                and december["bulletins"]["2025"]["status"] == "no_data"
            )
            for start, offset, target in [
                ("2023-12-29", 1, "2024-01-02"),
                ("2024-01-02", -1, "2023-12-29"),
            ]:
                data, failed = await query(action="workday_offset", date=start, offset=offset)
                assert not failed and data["date"] == target
            no_data, failed = await query(action="day", date="2090-01-01")
            assert not failed and no_data["is_workday"] is None and no_data["iso_weekday"]
            unknown, failed = await query(
                action="workday_offset", date="2089-12-31", offset=1, at_time="09:00"
            )
            assert failed and "trigger_at" not in unknown and calls.count(2090) == 1

        async def offline(year):
            raise OSError("offline")

        with SQLiteStore(path) as store:
            cached = HolidayCalendar(store.holiday_years, offline)
            assert (await cached.year(2023)).status == "published"
            store.holiday_years.save(
                2025, time() - 2 * 86400, bulletin(2025, [("2025-01-01", "元旦", True)])
            )
            assert (await cached.year(2025)).status == "stale_cache"
            assert (await cached.year(2027)).status == "unavailable"
            documents[2025] = b'{"year": 2025, "days": []}'
            damaged = HolidayCalendar(store.holiday_years, fetch)
            assert (await damaged.year(2025)).status == "stale_cache"

    asyncio.run(run())


async def check_chat_reminder(directory, start, target):
    prompt = "下一个工作日上午九点提醒我"
    clock = datetime.fromisoformat(start)
    results = {}

    def reply(data):
        last = data["messages"][-1]
        if last["role"] == "tool":
            results[last["tool_call_id"]] = json.loads(last["content"])
        if not results:
            assert prompt in str(last["content"])
            assert {"calendar_query", "time_query"} <= {
                t["function"]["name"] for t in data["tools"]
            }
            name, args = (
                "calendar_query",
                {"action": "workday_offset", "offset": 1, "at_time": "09:00"},
            )
        elif "schedule_write" not in results:
            name, args = (
                "schedule_write",
                {
                    "operation": "create",
                    "title": prompt,
                    "trigger_at": results["calendar_query"]["trigger_at"],
                },
            )
        else:
            return {"delta": {"content": "提醒已创建"}, "finish_reason": "stop"}
        return {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": name,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            },
            "finish_reason": "tool_calls",
        }

    async with (
        media_service(directory, model_reply=reply, clock=lambda: clock) as (url, _, _),
        ClientSession(
            url, headers={"X-Device-ID": "desktop", "Authorization": "Bearer desktop"}
        ) as client,
    ):
        response = await client.post("/v1/messages", json={"message_id": "remind", "text": prompt})
        assert response.status == 202
        job_id = (await response.json())["job_id"]
        async with asyncio.timeout(30):
            while True:
                job = await (await client.get(f"/v1/jobs/{job_id}")).json()
                if job["status"] not in {"queued", "running"}:
                    assert job["status"] == "completed", job
                    break
                await asyncio.sleep(0.03)
        assert results["calendar_query"]["trigger_at"] == target
        saved = results["schedule_write"]["item"]
        assert saved["trigger_at"] == target and saved["title"] == prompt
        calendar = await client.get(
            "/v1/schedule",
            params={
                "start": target[:10] + "T00:00:00+08:00",
                "end": target[:10] + "T23:59:59+08:00",
            },
        )
        assert calendar.status == 200
        (item,) = (await calendar.json())["items"]
        assert item["id"] == saved["id"] and item["status"] == "scheduled"
        assert datetime.fromisoformat(item["trigger_at"]) == datetime.fromisoformat(target)

    # Reopen the same calendar and advance its clock across the scheduled minute.
    instant = datetime.fromisoformat(target) - timedelta(seconds=1)
    with SQLiteStore(directory / "events.db", clock=lambda: instant) as store:
        assert store.schedules.dispatch_due_schedules() == 0
        instant += timedelta(seconds=1)
        assert store.schedules.dispatch_due_schedules() == 1
        assert store.schedules.dispatch_due_schedules() == 0
        triggered = store.schedules.get_schedule(user_id="u", conversation_id="c", id=saved["id"])
        assert triggered.status == "triggered" and triggered.triggered_at == instant


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("2026-11-20T18:00:00+08:00", "2026-11-23T09:00:00+08:00"),
        ("2026-10-09T18:00:00+08:00", "2026-10-10T09:00:00+08:00"),
    ],
)
def test_chat_creates_next_workday_reminder(tmp_path, monkeypatch, start, target):
    async def fetch(year):
        assert year == 2026
        return bulletin(2026, [("2026-10-10", "国庆节", False)])

    monkeypatch.setattr(holidays, "download_year", fetch)
    asyncio.run(check_chat_reminder(tmp_path, start, target))
