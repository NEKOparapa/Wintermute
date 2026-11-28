import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from purin.agent import (
    AgentContext,
    AgentRunner,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
)
from purin.agent.tools.conversation.replies import QuickReplyRequest, conversation_reply_tools
from purin.events.core.types import Device, EventError, EventInput, OutputRecord
from purin.events.ingress import EventGateway
from purin.events.runtime.scheduler import RunScheduler
from purin.events.storage.sqlite import SQLiteStore


def gateway_for(store: SQLiteStore) -> EventGateway:
    gateway = EventGateway(store)
    gateway.register_device(Device(user_id="user", device_id="phone"))
    gateway.register_conversation("room", "user")
    return gateway


def test_quick_replies_are_delivered_while_running() -> None:
    async def exercise() -> None:
        delivered: list[str] = []
        received = asyncio.Event()
        finish = asyncio.Event()

        class Model:
            calls = 0

            async def stream(self, context: AgentContext) -> AsyncIterator[AssistantMessage]:
                self.calls += 1
                if self.calls == 1:
                    yield AssistantMessage(
                        tool_calls=(
                            ToolCall("one", "quick_reply", {"messages": ["第一段"]}),
                            ToolCall("two", "quick_reply", {"messages": ["第二段", "第三段"]}),
                        )
                    )
                else:
                    assert isinstance(context.messages[-1], ToolResultMessage)
                    await finish.wait()
                    yield AssistantMessage("最终结果")

        def deliver(output: OutputRecord) -> None:
            delivered.append(output.text)
            if len(delivered) == 3:
                received.set()

        with SQLiteStore() as store:
            gateway = gateway_for(store)
            model = Model()
            runner = AgentRunner(model, tools=conversation_reply_tools())
            async with RunScheduler(
                gateway, lambda _: runner, delivery_retry_interval=120
            ) as sched:
                sched.output_router.connect("phone", deliver)
                receipt = await gateway.conversation.connect("phone", "room").send("开始")
                await asyncio.wait_for(received.wait(), timeout=5)
                assert receipt.job_id and store.sessions.get_job(receipt.job_id).status == "running"
                assert delivered == ["第一段", "第二段", "第三段"]
                finish.set()
                await sched.wait_until_idle()
                assert model.calls == 2
                assert delivered == ["第一段", "第二段", "第三段", "最终结果"]
                rows, _ = store.history.browse_history(
                    user_id="user", conversation_id="room", view="dialogue"
                )
                assert [r.text for r in reversed(rows)] == ["开始", *delivered]
                rows, _ = store.history.browse_history(
                    user_id="user", conversation_id="room", view="chat"
                )
                assert len(rows) == len({r.id for r in rows})

    asyncio.run(exercise())


@pytest.mark.parametrize("send_first", [False, True])
def test_end_session_settles_batch_and_preserves_new_input(
    monkeypatch: pytest.MonkeyPatch, send_first: bool
) -> None:
    async def exercise() -> None:
        class Model:
            calls = 0

            async def stream(self, context: AgentContext) -> AsyncIterator[AssistantMessage]:
                self.calls += 1
                if self.calls == 1:
                    calls = []
                    if send_first:
                        calls.append(ToolCall("reply", "quick_reply", {"messages": ["已核对"]}))
                    yield AssistantMessage(
                        tool_calls=(
                            *calls,
                            ToolCall("stop", "end_session", {}),
                            ToolCall("after", "quick_reply", {"messages": ["不会发送"]}),
                        )
                    )
                else:
                    assert "新问题" in context.messages[-1].text
                    yield AssistantMessage("新答复")

        with SQLiteStore() as store:
            gateway = gateway_for(store)
            model = Model()
            start = store.sessions.try_start_tool

            def start_with_input(run_id: str, call_id: str, *, owner_id: str) -> bool:
                started = start(run_id, call_id, owner_id=owner_id)
                if started and call_id == "stop":
                    # An independent producer commits just after the tool has started.
                    store.append(
                        EventInput(
                            device_id="phone",
                            layer="conversation",
                            type="user.message",
                            producer_event_id="followup",
                            conversation_id="room",
                            reply_device_id="phone",
                            payload={"text": "新问题"},
                        )
                    )
                return started

            monkeypatch.setattr(store.sessions, "try_start_tool", start_with_input)
            runner = AgentRunner(model, tools=conversation_reply_tools())
            async with RunScheduler(gateway, lambda _: runner) as sched:
                receipt = await gateway.notification.connect("phone", "room").publish(
                    "backup.completed", {"text": "备份完成"}
                )
                await sched.wait_until_idle()
                assert (
                    receipt.job_id and store.sessions.get_job(receipt.job_id).status == "completed"
                )
                expected = ["已核对"] if send_first else []
                assert [
                    o.text for o in store.outputs.list_outputs(job_id=receipt.job_id)
                ] == expected
                transcript = store.sessions.get_run_messages(receipt.job_id)
                assert transcript[-2] == ToolResultMessage("stop", "end_session", "终止成功")
                assert isinstance(transcript[-1], ToolResultMessage) and transcript[-1].is_error
                assert [o.text for o in store.outputs.list_outputs()] == [*expected, "新答复"]
                assert model.calls == 2

    asyncio.run(exercise())


def test_quick_reply_rejects_empty_paragraphs() -> None:
    for messages in ([], ["有内容", "  "]):
        with pytest.raises(ValidationError):
            QuickReplyRequest(messages=messages)


def test_output_and_dialogue_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with SQLiteStore(path) as store:
        gateway = gateway_for(store)
        receipt = asyncio.run(gateway.conversation.connect("phone", "room").send("旧问题"))
        db = store._connection
        db.execute(
            "INSERT INTO outputs(output_id,event_id,job_id,user_id,conversation_id,device_id,"
            "layer,text,status) VALUES('old',?,?,'user','room','phone','conversation',"
            "'旧答复','delivered')",
            (receipt.event.event_id, receipt.job_id),
        )
        db.execute(
            "INSERT INTO dialogue_entries(user_id,conversation_id,output_id) "
            "VALUES('user','room','old')"
        )
    with SQLiteStore(path) as store:
        assert store.outputs.get_output("old").status == "delivered"
        rows, _ = store.history.browse_history(
            user_id="user", conversation_id="room", view="dialogue"
        )
        assert [r.text for r in rows] == ["旧答复", "旧问题"]
        assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()


def test_unsupported_database_is_left_intact(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE saved (value TEXT)")
        db.execute("INSERT INTO saved VALUES ('existing data')")
        db.execute("PRAGMA user_version=40")
    before = path.read_bytes()
    with pytest.raises(EventError, match="Unsupported database version 40"):
        SQLiteStore(path)
    assert path.read_bytes() == before
