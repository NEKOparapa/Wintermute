"""Exercise chat and capture through the normal service while summary inference is in flight."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from model_text import event_fields

from wintermute.agent import AgentContext, AssistantMessage, ModelEvent, ToolContext, UserMessage
from wintermute.config import AppConfig
from wintermute.config.factory import ModelSet, create_store
from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.integrations.activity_search import ActivitySearch, activity_search_tool
from wintermute.interfaces.service import ConversationService


class SummaryModel:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        assert not context.tools
        self.entered.set()
        await self.release.wait()
        yield AssistantMessage("整理了项目验收清单")


class ChatModel:
    def __init__(self):
        self.requests: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        self.requests.append(context)
        yield AssistantMessage("已收到。")


def timelines(request: AgentContext) -> list[UserMessage]:
    return [
        m
        for m in request.messages
        if isinstance(m, UserMessage) and event_fields(m.text).get("来源") == "活动时间线"
    ]


@pytest.mark.parametrize("chat_during_summary", [True, False])
async def test_summary_runs_independently_of_capture_and_chat(
    tmp_path: Path, chat_during_summary, monkeypatch
):
    settings = AppConfig.model_validate(
        {
            "events": {
                "database_path": str(tmp_path / "events.db"),
                "poll_interval": 0.015,
                "timeline_compression": {
                    "raw_tokens": 400,
                    "ai_threshold": 100,
                    "summary_tokens": 64,
                    "delivery_tokens": 480,
                },
            },
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {
                            "terminal_id": "desktop",
                            "user_id": "user",
                            "conversation_id": "work",
                            "token": "test",
                        }
                    ],
                }
            },
        }
    )
    main, summary = ChatModel(), SummaryModel()
    with create_store(settings.events) as store:
        persisted = asyncio.Event()
        finish = store.finish_timeline_batch

        def record_completion(batch_id, text):
            finish(batch_id, text)
            persisted.set()

        monkeypatch.setattr(store, "finish_timeline_batch", record_completion)
        service = ConversationService(settings, ModelSet(main, {}, timeline_summary=summary), store)
        async with asyncio.timeout(8), service:
            sensor = service.gateway.perception.connect("desktop", conversation_id="work")
            chat = service.gateway.conversation.connect("desktop", "work")
            await sensor.observe("browser.page", "项目验收清单：" + "核对下一项文档；" * 55)
            await summary.entered.wait()
            assert not main.requests
            await sensor.observe("active_document", "最新计划.md")
            if not chat_during_summary:
                summary.release.set()
                await persisted.wait()
            await chat.send("我刚才打开了哪个文件？")
            await service.scheduler.wait_until_idle()
            assert len(main.requests) == 1
            (first,) = timelines(main.requests[0])
            assert len(first.model_text().encode()) <= 480
            assert "最新计划.md" in first.text
            assert ("整理了项目验收清单" in first.text) == (not chat_during_summary)
            assert any(t.name == "search_activity" for t in main.requests[0].tools)
            summary.release.set()
            await persisted.wait()
            assert store.pending_timeline("work", "user") is None
            await chat.send("还有其他更新吗？")
            await service.scheduler.wait_until_idle()
            assert len(main.requests) == 2
            assert timelines(main.requests[1]) == [first]
        assert store.pending_timeline("work", "user") is None
    with SQLiteStore(tmp_path / "events.db") as restored:
        assert restored.pending_timeline("work", "user") is None
        assert [
            m.model_text()
            for m in restored.history("work", "user")
            if isinstance(m, UserMessage) and m.source == "timeline"
        ] == [first.text]


async def test_detail_lookup_is_scoped_and_returns_raw_times_and_titles():
    base = datetime(2026, 11, 15, 1, tzinfo=UTC)
    with SQLiteStore(":memory:") as store:
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="desktop", user_id="user"))
        for room in ("work", "private"):
            gateway.register_conversation(room, "user")
            sensor = gateway.perception.connect("desktop", conversation_id=room)
            for minute in [0, 15, 30]:
                await sensor.observe(
                    "browser.page",
                    {"title": f"{room} 项目 {minute}"},
                    occurred_at=base + timedelta(minutes=minute),
                )
        tool = activity_search_tool(store, "work")
        query = ActivitySearch(
            start=base, end=base + timedelta(minutes=30), keyword="项目", limit=1
        )
        first = await tool.execute(query, ToolContext("lookup", 1))
        assert "work 项目 0" in first.content and "offset=1" in first.content
        last = await tool.execute(query.model_copy(update={"offset": 1}), ToolContext("lookup2", 2))
        assert "work 项目 15" in last.content and "01:15" in last.content
        assert "private" not in last.content and "offset=" not in last.content


async def test_day_group_only_replaces_six_complete_visible_batches():
    policy = TimelinePolicy(raw_tokens=1, ai_threshold=1, delivery_tokens=400)
    base = datetime(2026, 11, 15, 1, tzinfo=UTC)
    with SQLiteStore(":memory:", timeline_policy=policy) as store:
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="desktop", user_id="user"))
        gateway.register_conversation("all", "user")
        sensor = gateway.perception.connect("desktop")
        for minute in range(6):
            if minute == 1:
                gateway.register_conversation("joined_later", "user")
            await sensor.observe(
                "browser.page",
                f"项目会议纪要的第{minute}页，等待补充",
                occurred_at=base + timedelta(minutes=minute),
            )
        group = store.next_timeline_batch()
        assert group and group.batch_id < 0 and "第5页" in group.rule_text
        store.retry_timeline_batch(group.batch_id, delay_seconds=0)
        assert store.next_timeline_batch().attempts == 1
        store.finish_timeline_batch(group.batch_id, "连续阅读六页会议纪要")
        full = store.pending_timeline("all", "user")
        partial = store.pending_timeline("joined_later", "user")
        assert full and "连续阅读六页会议纪要" in full.text
        assert (
            partial and "连续阅读六页会议纪要" not in partial.text and "第0页" not in partial.text
        )
        assert "第5页" in partial.text
