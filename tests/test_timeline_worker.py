"""Exercise the model worker's restart, failure and request isolation boundaries."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from wintermute.agent import AgentContext, AgentRunner, AssistantMessage, ModelEvent, ToolCall
from wintermute.config import AppConfig, EventSettings, TimelineCompressionSettings, factory
from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.core.perception_tokens import count_tokens
from wintermute.events.runtime.timeline_worker import TimelineWorker


class Probe:
    def __init__(self, final: AssistantMessage, *, block: bool = False) -> None:
        self.final = final
        self.contexts: list[AgentContext] = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        self.contexts.append(context)
        self.started.set()
        try:
            await self.release.wait()
            yield self.final
        finally:
            self.closed.set()


def settings(path: Path) -> EventSettings:
    return EventSettings(
        database_path=str(path),
        timeline_compression=TimelineCompressionSettings(
            raw_tokens=1, ai_threshold=1, summary_tokens=80, delivery_tokens=400
        ),
    )


def ingress(store: SQLiteStore) -> EventGateway:
    gateway = EventGateway(store)
    gateway.register_terminal(Terminal(terminal_id="pc", user_id="owner"))
    gateway.register_conversation("chat", "owner")
    return gateway


async def test_shutdown_does_not_replay_observations_already_delivered_during_inference(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path / "activity.db")
    slow = Probe(AssistantMessage("old result"), block=True)
    main = Probe(AssistantMessage("好的"))
    with factory.create_store(config) as store:
        gateway = ingress(store)
        device = gateway.perception.connect("pc")
        await device.observe("location", "会议室" * 420)
        async with (
            asyncio.timeout(4),
            factory.create_scheduler(
                config, gateway, lambda _: AgentRunner(main), timeline_summary_model=slow
            ) as scheduler,
        ):
            await slow.started.wait()
            assert not store.list_jobs()
            await device.observe("location", "书房")
            chat = await gateway.conversation.connect("pc", "chat").send("现在在哪？")
            assert chat.job_id is not None
            assert (await scheduler.wait(chat.job_id)).status == "completed"
            assert "书房" in repr(main.contexts[-1].messages)
            assert not slow.closed.is_set()
            assert "书房" not in repr(slow.contexts[0])
            assert not slow.contexts[0].tools and len(slow.contexts[0].messages) == 1
            assert "现在在哪" not in repr(slow.contexts[0])
            history = store.history("chat", "owner")
        assert slow.closed.is_set()

    recovered = Probe(AssistantMessage("简短的会议摘要"))
    with factory.create_store(config) as store:
        worker = TimelineWorker(store, recovered)
        assert not await worker.process_next()
        assert not recovered.contexts
        assert store.history("chat", "owner") == history
        assert len(store.list_jobs()) == 1


@pytest.mark.parametrize("failure", ["timeout", "tool", "empty", "incomplete"])
async def test_bad_model_delays_only_failed_batch_and_closes_stream(
    tmp_path: Path, failure: str
) -> None:
    config = settings(tmp_path / "activity.db")
    with factory.create_store(config) as store:
        gateway = ingress(store)
        gateway.register_conversation("other", "owner")
        await gateway.perception.connect("pc", conversation_id="chat").observe(
            "location", "第一间会议室" * 420
        )
        await gateway.perception.connect("pc", conversation_id="other").observe(
            "location", "第二间会议室" * 420
        )
        final = {
            "tool": AssistantMessage(tool_calls=(ToolCall("1", "unexpected", {}),)),
            "incomplete": AssistantMessage("未完成", finish_reason="length"),
        }.get(failure, AssistantMessage())
        model = Probe(final, block=failure == "timeout")
        assert await TimelineWorker(store, model, model_timeout=0.05).process_next()
        assert model.closed.is_set()
        ready = store.next_timeline_batch()
        assert "第二间会议室" in ready.rule_text
        assert await TimelineWorker(store, Probe(AssistantMessage("第二间会议室"))).process_next()
        assert store.next_timeline_batch() is None
        assert not store.list_jobs()


async def test_timeline_model_selection_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = []
    closed = []

    @asynccontextmanager
    async def model_for(setting, **kwargs):
        probe = Probe(AssistantMessage(setting.model))
        opened.append(probe)
        try:
            yield probe
        finally:
            closed.append(probe)

    monkeypatch.setattr(factory, "open_model", model_for)
    config = AppConfig.model_validate(
        {
            "events": {
                "database_path": ":memory:",
                "timeline_compression": {"summary_model": {"model": "timeline-only"}},
            }
        }
    )
    with factory.create_store(config.events) as store:
        gateway = ingress(store)
        async with factory.open_models(config) as models:
            scheduler = factory.create_scheduler(
                config.events,
                gateway,
                lambda _: AgentRunner(models.main),
                slice_model=models.main,
                timeline_summary_model=models.timeline_summary,
            )
            assert len(opened) == 2 and not closed
            assert scheduler.timeline_worker.model is opened[1]
            assert opened[1].final == AssistantMessage("timeline-only")
        assert closed == opened[::-1]

        audio, main = Probe(AssistantMessage("audio")), Probe(AssistantMessage("main"))
        for audio_model, expected in [(audio, audio), (None, main)]:
            scheduler = factory.create_scheduler(
                EventSettings(),
                gateway,
                lambda _: AgentRunner(main),
                slice_model=main,
                audio_summary_model=audio_model,
            )
            assert scheduler.timeline_worker.model is expected


async def test_timeline_displays_complete_model_reply_when_context_copy_is_shorter(tmp_path: Path):
    config = settings(tmp_path / "activity.db")
    original = "完整模型输出。" * 32 + "\n最后一行。"
    with factory.create_store(config) as store:
        device = ingress(store).perception.connect("pc")
        receipt = await device.observe("page", "工作记录" * 500)
        batch = store.next_timeline_batch()
        assert batch is not None
        assert await TimelineWorker(store, Probe(AssistantMessage(original))).process_next()
        store.finish_timeline_batch(batch.batch_id, "重复回执")
    with factory.create_store(config) as store:
        target = f"round-{batch.batch_id}"
        page = store.trace_members("owner", "chat", target, None, 10)
        assert page["round"]["summary"] == original
        detail = store.perception_stages("owner", "chat", receipt.event.event_id, ai_enabled=True)
        assert detail["rounds"][0]["summary"] == original
        rows, _ = store.browse_history(
            user_id="owner", conversation_id="chat", view="perceptiontimeline"
        )
        row = next(r for r in rows if r.id == target)
        assert row.text == original and row.summary_round["usage"]["tokens"] == count_tokens(
            original
        )
        pending = store.pending_timeline("chat", "owner")
        assert pending and "完整模型输出" in pending.text and "最后一行" not in pending.text
        model = Probe(AssistantMessage("下一轮完整输出"))
        await EventGateway(store).perception.connect("pc").observe("page", "新记录" * 5000)
        assert await TimelineWorker(store, model).process_next()
        assert (
            model.contexts[-1]
            .messages[0]
            .text.startswith(f"已有摘要：\n{original}\n新增观测（按发生时间）：\n")
        )


async def test_large_observation_reaches_model_in_bounded_pages_including_tail(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path / "activity.db")
    with factory.create_store(config) as store:
        device = ingress(store).perception.connect("pc")
        await device.observe("page", "开头关键词" + "资料" * 5000 + "结尾关键词")
        batch = store.next_timeline_batch()
        assert batch is not None
        model = Probe(AssistantMessage("一页的结论"))
        assert await TimelineWorker(store, model).process_next()
        source_requests = [context.messages[0].text for context in model.contexts]
        # The concatenated first pass consumes the complete immutable source, not a prefix.
        assert "".join(source_requests[:2]) == batch.rule_text
        assert "结尾关键词" in source_requests[1]
        assert all(len(page.encode()) <= 16_384 for page in source_requests)
