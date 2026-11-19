import asyncio
import json

import pytest
from aiohttp import ClientSession
from pydantic import ValidationError
from test_chat_media import media_service

from wintermute.agent import AssistantMessage, ToolContext
from wintermute.agent.tools import (
    BackgroundSubagentManager,
    SubagentConfig,
    SubagentControlInput,
    SubagentStatusInput,
)


def test_action_arguments_are_validated_before_execution():
    assert SubagentStatusInput.model_validate({}, strict=True).task_id is None
    assert set(SubagentStatusInput.model_json_schema()["properties"]) == {"task_id"}
    for payload in [{"task_id": " "}, {"action": "start"}, {"agent": "reader", "task": "work"}]:
        with pytest.raises(ValidationError):
            SubagentStatusInput.model_validate(payload, strict=True)
    invalid = [
        {},
        {"action": "unknown"},
        {"action": "start", "agent": "reader"},
        {"action": "start", "tasks": []},
        {"action": "start", "tasks": [{"agent": "reader", "task": "work"}] * 9},
        {"action": "start", "agent": "reader", "task": "work", "task_id": "x"},
        {
            "action": "start",
            "agent": "reader",
            "task": "work",
            "tasks": [{"agent": "reader", "task": "work"}],
        },
        {"action": "status"},
        {"action": "cancel"},
        {"action": "cancel", "task_id": " "},
        {"action": "cancel", "task_id": "x", "tasks": []},
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            SubagentControlInput.model_validate(payload, strict=True)


def test_batch_status_cancel_and_completion_notifications():
    async def check():
        entered = asyncio.Event()
        cleanup = asyncio.Event()
        notices = []

        class Worker:
            async def stream(self, context):
                if context.messages[-1].text == "wait":
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        await cleanup.wait()
                yield AssistantMessage("finished")

        async def publish(state):
            notices.append(state)

        async with BackgroundSubagentManager(
            [SubagentConfig("reader", Worker())], notification_publisher=publish
        ) as manager:
            tools = {tool.name: tool for tool in manager.tools}
            assert set(tools) == {"subagent_status", "subagent_control"}

            async def call(name, **arguments):
                tool = tools[name]
                params = tool.parameters.model_validate(arguments, strict=True)
                result = await tool.execute(params, ToolContext("batch-call", 1))
                assert not result.is_error
                return json.loads(result.content)

            try:
                batch = [{"agent": "reader", "task": "done"}, {"agent": "reader", "task": "wait"}]
                with pytest.raises(ValueError, match="Unknown subagents"):
                    await call(
                        "subagent_control",
                        action="start",
                        tasks=[batch[0], {"agent": "unknown", "task": "work"}],
                    )
                assert await call("subagent_status") == {"tasks": []}
                jobs = (await call("subagent_control", action="start", tasks=batch))["tasks"]
                done, pending = [row["task_id"] for row in jobs]
                assert done != pending
                await entered.wait()
                assert (await call("subagent_status", task_id=pending))["task"][
                    "status"
                ] == "running"
                assert (await manager.wait(done)).output == "finished"
                cancelled = await call("subagent_control", action="cancel", task_id=pending)
                assert cancelled["cancel_requested"] and cancelled["task"]["status"] == "cancelling"
                assert not (await call("subagent_control", action="cancel", task_id=pending))[
                    "cancel_requested"
                ]
            finally:
                cleanup.set()
            await manager.wait_until_idle()
            final = (await call("subagent_status"))["tasks"]
            assert [(row["task_id"], row["status"]) for row in final] == [
                (done, "completed"),
                (pending, "cancelled"),
            ]
            assert sorted(state.status for state in notices) == ["cancelled", "completed"]
            assert all(state.call_id == "batch-call" for state in notices)

    asyncio.run(asyncio.wait_for(check(), timeout=5))


def test_main_conversation_can_start_and_query_through_separate_tools(tmp_path):
    async def check():
        delivered = asyncio.Event()
        answers = []
        calls = 0

        def reply(data):
            nonlocal calls
            last = data["messages"][-1]
            if last["role"] == "tool":
                answers.append(json.loads(last["content"]))
            elif last["content"] == "summarize":
                return {"delta": {"content": "child-result"}, "finish_reason": "stop"}
            elif "child-result" in last["content"]:
                delivered.set()
            else:
                names = [tool["function"]["name"] for tool in data["tools"]]
                assert {name for name in names if name.startswith("subagent")} == {
                    "subagent_control",
                    "subagent_status",
                }
                assert {name for name in names if name.startswith("schedule")} == {
                    "schedule_read",
                    "schedule_write",
                }
                arguments = (
                    {"action": "start", "agent": "reader", "task": "summarize"}
                    if calls == 0
                    else {"task_id": answers[0]["tasks"][0]["task_id"]}
                )
                tool_name = "subagent_control" if calls == 0 else "subagent_status"
                calls += 1
                return {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call-{calls}",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            return {"delta": {"content": "ok"}, "finish_reason": "stop"}

        async with (
            media_service(tmp_path, model_reply=reply, subagents=["reader"]) as (url, _, _),
            ClientSession(
                url, headers={"X-Device-ID": "desktop", "Authorization": "Bearer desktop"}
            ) as client,
        ):
            for message_id in ("start", "status"):
                if message_id == "status":
                    await asyncio.wait_for(delivered.wait(), 5)
                receipt = await client.post(
                    "/v1/messages", json={"message_id": message_id, "text": message_id}
                )
                assert receipt.status == 202
                job_id = (await receipt.json())["job_id"]
                async with asyncio.timeout(5):
                    while True:
                        job = await (await client.get(f"/v1/jobs/{job_id}")).json()
                        if job["status"] not in ("running", "queued"):
                            assert job["status"] == "completed", job
                            break
                        await asyncio.sleep(0.02)
            assert calls == 2
            assert answers[1]["task"]["task_id"] == answers[0]["tasks"][0]["task_id"]
            assert answers[1]["task"]["output"] == "child-result"
            assert answers[1]["task"]["status"] == "completed"

    asyncio.run(check())
