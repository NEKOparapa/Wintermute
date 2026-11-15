"""A main-agent tool call appears in the desktop calendar and wakes the same conversation."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession
from model_text import event_fields

from wintermute.agent import (
    AgentContext,
    AssistantMessage,
    ModelEvent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.config import AppConfig
from wintermute.config.factory import ModelSet
from wintermute.events import SQLiteStore
from wintermute.interfaces.service import ConversationService


class MeetingModel:
    def __init__(self) -> None:
        self.notifications: list[str] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        last = context.messages[-1]
        if isinstance(last, ToolResultMessage):
            assert last.name == "schedule" and not last.is_error, last.content
            yield AssistantMessage("已安排。")
        else:
            fields = event_fields(last.text)
            if fields["来源"] == "后台通知 · 时间表":
                self.notifications.append(fields["正文"])
                yield AssistantMessage("准备评审清单。")
            else:
                assert fields["来源"].startswith("用户原话") and fields["正文"] == "安排评审准备"
                assert "schedule" in {tool.name for tool in context.tools}
                yield AssistantMessage(
                    tool_calls=(
                        ToolCall(
                            "new-meeting",
                            "schedule",
                            {
                                "operation": "create",
                                "title": "评审准备",
                                "content": "核对周五的交付清单",
                                "trigger_at": "2026-11-16T09:00:00+08:00",
                                "action": "notify_agent",
                            },
                        ),
                    )
                )


async def test_main_tool_to_shared_calendar_to_self_notification():
    now = datetime(2026, 11, 16, 0, 55, tzinfo=UTC)
    config = AppConfig.model_validate(
        {
            "events": {"poll_interval": 0.01, "lease_seconds": 900},
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {
                            "terminal_id": name,
                            "token": name,
                            "conversation_id": room,
                            "user_id": user,
                        }
                        for name, room, user in [
                            ("phone", "work", "owner"),
                            ("desktop", "work", "owner"),
                            ("private", "another", "owner"),
                            ("guest", "guest-room", "guest"),
                        ]
                    ],
                }
            },
        }
    )
    model = MeetingModel()
    with SQLiteStore(":memory:", clock=lambda: now) as store:
        service = ConversationService(config, ModelSet(model, {}), store)
        async with asyncio.timeout(10), service, ClientSession() as client:
            assert service.http
            host, port = service.http.addresses[0]
            base = f"http://{host}:{port}/v1"
            bounds = {"start": "2026-11-16T09:00:00+08:00", "end": "2026-11-17T00:00:00+08:00"}

            def headers(name):
                return {"X-Terminal-ID": name, "Authorization": "Bearer " + name}

            async def calendar(name, params=None):
                async with client.get(
                    base + "/schedule",
                    params=bounds if params is None else params,
                    headers=headers(name),
                ) as r:
                    assert r.status == 200 and "no-store" in r.headers["Cache-Control"]
                    return await r.json()

            async with client.post(
                base + "/messages",
                headers=headers("phone"),
                json={"message_id": "request", "text": "安排评审准备"},
            ) as r:
                assert r.status == 202
                job_id = (await r.json())["job_id"]
            assert (await service.scheduler.wait(job_id)).status == "completed"
            await service.scheduler.wait_until_idle()
            (entry,) = (await calendar("desktop"))["items"]
            assert entry["title"] == "评审准备" and entry["status"] == "scheduled"
            assert datetime.fromisoformat(entry["trigger_at"]) == now + timedelta(minutes=5)
            assert await calendar("phone") == await calendar("desktop")
            for name in ("guest", "private"):
                assert (await calendar(name))["items"] == []
            assert (await calendar("desktop", {"start": now.isoformat(), "end": bounds["start"]}))[
                "items"
            ] == []
            assert not model.notifications
            assert (await client.get(base + "/schedule", params=bounds)).status == 401
            for bad in (
                {},
                {**bounds, "start": "2026-11-16"},
                {**bounds, "end": bounds["start"]},
                {**bounds, "limit": "0"},
                {**bounds, "conversation_id": "another"},
            ):
                assert (
                    await client.get(base + "/schedule", params=bad, headers=headers("desktop"))
                ).status == 400

            now += timedelta(minutes=5)
            await service.scheduler.wait_until_idle()
            (fired,) = (await calendar("desktop"))["items"]
            assert fired["id"] == entry["id"] and fired["status"] == "triggered"
            assert datetime.fromisoformat(fired["triggered_at"]) == now
            assert len(model.notifications) == 1 and "核对周五的交付清单" in model.notifications[0]
            async with client.get(
                base + "/history", params={"view": "notification"}, headers=headers("desktop")
            ) as r:
                records = (await r.json())["items"]
                (log,) = [row for row in records if row["role"] == "user"]
                assert log["text"] == model.notifications[0] and "时间表" in log["source"]
            for name in ("desktop", "phone"):
                async with client.get(base + "/outputs", headers=headers(name)) as r:
                    assert all(
                        out["layer"] != "notification" for out in (await r.json())["outputs"]
                    )


async def test_due_notification_reaches_main_agent_during_reply_generation():
    now = datetime(2026, 11, 16, 1, tzinfo=UTC)
    entered, interrupted = asyncio.Event(), asyncio.Event()
    seen = []

    class MidReplyModel:
        async def stream(self, context):
            if not entered.is_set():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    interrupted.set()
            seen.extend(
                m.text
                for m in context.messages
                if isinstance(m, UserMessage) and "来源：后台通知 · 时间表" in m.text
            )
            yield AssistantMessage("已核对。")

    config = AppConfig.model_validate(
        {
            "events": {"poll_interval": 0.01},
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {
                            "terminal_id": "pc",
                            "token": "fixture",
                            "conversation_id": "room",
                            "user_id": "u",
                        }
                    ],
                }
            },
        }
    )
    with SQLiteStore(":memory:", clock=lambda: now) as store:
        service = ConversationService(config, ModelSet(MidReplyModel(), {}), store)
        async with asyncio.timeout(5), service:
            item = store.create_schedule(
                user_id="u",
                conversation_id="room",
                title="检查材料",
                trigger_at=now + timedelta(seconds=1),
            )
            await service.gateway.conversation.connect("pc", "room").send("帮我整理会议纪要")
            await entered.wait()
            now += timedelta(seconds=1)
            await service.scheduler.wait_until_idle()
            assert interrupted.is_set() and any("检查材料" in text for text in seen)
            (event,) = store.list_events("notification", user_id="u")
            assert event.correlation_id == item.id and event.reply_terminal_id is None
