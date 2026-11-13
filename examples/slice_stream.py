"""离线演示切片入库、当日流与主 Agent 上下文，无需 API Key。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from wintermute.agent import AgentContext, AgentRunner, AssistantMessage, ModelEvent, UserMessage
from wintermute.events import EventGateway, RunScheduler, SQLiteStore, Terminal


class DemoModel:
    """使用已知的活动历史，按观察发生日期回答今天的问题。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        updates = [
            m.text.rpartition("\n\n---\n") for m in context.messages if isinstance(m, UserMessage)
        ]
        (timeline,) = [
            body for body, _, footer in updates if footer.startswith("来源：活动时间线\n")
        ]
        zone = ZoneInfo("Asia/Shanghai")
        today = datetime.now(zone).date()
        for line in timeline.splitlines():
            if " | 切片 · glasses" in line:
                timestamp = line.partition(" | ")[0].split(" ", 1)[0]
                day = datetime.fromisoformat(timestamp).astimezone(zone).date()
                assert ("检查昨日采购清单" in line) == (day < today)
        assert "完成切片层接入" in timeline and "检查昨日采购清单" in timeline
        assert not context.reference_messages
        yield AssistantMessage("今天讨论了演示安排，并决定先完成切片层接入。")


async def main() -> None:
    with TemporaryDirectory(prefix="wintermute-slices-") as directory:
        with SQLiteStore(Path(directory, "events.sqlite3")) as store:
            gateway = EventGateway(store)
            gateway.register_terminal(
                Terminal(terminal_id="desktop", user_id="user", allowed_layers=("conversation",))
            )
            gateway.register_terminal(
                Terminal(terminal_id="glasses", user_id="user", allowed_layers=("slice",))
            )
            gateway.register_conversation("work", "user")
            chat = gateway.conversation.connect("desktop", "work")
            # 共享切片可以用于该用户的多个会话；私有资料可指定 conversation_id。
            slices = gateway.slice.connect("glasses", source_id="transcript")
            now = datetime.now(UTC)
            first_time = now
            text = "讨论了明天的演示，决定先完成切片层接入。"
            receipt = await slices.record(
                text,
                producer_event_id="segment-001",
                occurred_at=first_time,
                metadata={"evidence_ref": "local:recording-001"},
            )
            duplicate = await slices.record(
                text,
                producer_event_id="segment-001",
                occurred_at=first_time,
                metadata={"evidence_ref": "local:recording-001"},
            )
            assert duplicate.duplicate and duplicate.event.event_id == receipt.event.event_id
            assert receipt.job_id is None
            await slices.record(
                text,
                producer_event_id="segment-002",
                occurred_at=now,
            )
            await slices.record(
                "检查昨日采购清单。",
                producer_event_id="yesterday-001",
                occurred_at=now - timedelta(days=1),
            )

            model = DemoModel()
            scheduler = RunScheduler(gateway, lambda _: AgentRunner(model))
            async with asyncio.timeout(10), scheduler:
                await scheduler.wait_for_slices(timeout=5)
                assert model.calls == 0
                status = store.get_slice_status(receipt.event.event_id, user_id="user")
                assert status.status == "completed"
                today = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                stream = store.list_slice_stream(
                    user_id="user",
                    conversation_id="work",
                    day=today,
                    timezone="Asia/Shanghai",
                )
                assert stream and sum(len(item.source_event_ids) for item in stream) == 2
                print(
                    "[当日切片流]", json.dumps([item.text for item in stream], ensure_ascii=False)
                )
                print("[独立处理] 原始切片已压缩，主 Agent 尚未运行。")

                question = "今天讨论了什么？"
                message = await chat.send(question)
                assert message.job_id is not None
                job = await scheduler.wait(message.job_id)
                assert job.status == "completed"
                outputs = store.list_outputs(user_id="user", job_id=job.job_id)
                print("[主 Agent]", outputs[0].text)
                history = store.history("work", "user")
                assert history[-2].source == "user" and history[-2].text == question
                (update,) = history[:-2]
                assert isinstance(update, UserMessage) and update.source == "timeline"
                assert len(store.list_jobs()) == 1 and len(outputs) == 1
                print("[会话历史] 两天的切片合并成一条时间线，用户提问后产生回复。")


if __name__ == "__main__":
    asyncio.run(main())
