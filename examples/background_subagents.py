"""演示后台子 Agent、主对话继续响应、状态查询和完成后的自动通知，无需 API Key。"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from wintermute.agent import (
    AgentContext,
    AgentEvent,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.tools import BackgroundSubagentManager, SubagentConfig, create_read_tool


class ReaderModel:
    """用门闩模拟仍在运行的子任务，随后通过真实工具读取临时文件。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        assert context.system_prompt == "你是资料员，只读取指定文件并返回摘要。"
        assert [tool.name for tool in context.tools] == ["read"]
        if len(context.messages) == 1:
            assert isinstance(context.messages[0], UserMessage)
            self.started.set()
            await self.release.wait()
            yield AssistantMessage(
                tool_calls=(ToolCall("read-note", "read", {"path": "note.txt"}),),
                finish_reason="tool_calls",
            )
        else:
            result = context.messages[-1]
            assert isinstance(result, ToolResultMessage) and not result.is_error
            answer = f"文件内容：{result.content.strip()}"
            yield TextDelta(answer)
            yield AssistantMessage(answer)


class CoordinatorModel:
    """启动后台任务后继续回答用户，并在新一轮中处理完成通知。"""

    def __init__(self) -> None:
        self.task_id: str | None = None
        self.notifications = 0

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        message = context.messages[-1]
        if isinstance(message, ToolResultMessage):
            assert not message.is_error
            payload = json.loads(message.content)
            if message.name == "subagent_start":
                self.task_id = payload["tasks"][0]["task_id"]
                yield AssistantMessage(f"后台任务已启动，ID：{self.task_id}。可以继续聊天。")
            elif message.name == "subagent_status":
                state = payload["task"]
                yield AssistantMessage(f"查询结果：{state['task_id']} 当前为 {state['status']}。")
            else:
                raise AssertionError(f"Unexpected tool result: {message.name}")
            return

        assert isinstance(message, UserMessage)
        body, _, footer = message.text.rpartition("\n\n---\n")
        if body == "启动后台任务":
            yield AssistantMessage(
                tool_calls=(
                    ToolCall(
                        "start-reader",
                        "subagent_start",
                        {"agent": "reader", "task": "读取 note.txt，返回文件摘要。"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        elif body == "现在还能聊吗":
            yield AssistantMessage("可以。资料员仍在后台工作，我可以继续处理你的消息。")
        elif body == "查询任务状态":
            assert self.task_id is not None
            yield AssistantMessage(
                tool_calls=(
                    ToolCall("query-reader", "subagent_status", {"task_id": self.task_id}),
                ),
                finish_reason="tool_calls",
            )
        else:
            assert footer.splitlines()[0] == "来源：后台通知 · reader"
            assert "文件内容" in body
            self.notifications += 1
            yield AssistantMessage(f"收到后台完成通知：{body}")


def display(event: AgentEvent) -> None:
    """同一订阅接收用户对话回复和后台通知自动唤醒后的回复。"""
    if event.type == "message_end" and isinstance(event.message, AssistantMessage):
        if event.message.text:
            print(f"[主 Agent] {event.message.text}")


async def main() -> None:
    with TemporaryDirectory(prefix="wintermute-background-") as workspace:
        await asyncio.to_thread(
            Path(workspace, "note.txt").write_text,
            "后台任务完成后，管理器会在主 Agent 空闲时自动投递结果。\n",
            encoding="utf-8",
        )
        reader = ReaderModel()
        coordinator = CoordinatorModel()
        background = BackgroundSubagentManager(
            [
                SubagentConfig(
                    "reader",
                    reader,
                    description="读取资料并返回摘要",
                    system_prompt="你是资料员，只读取指定文件并返回摘要。",
                    tools=[create_read_tool(workspace)],
                    timeout=10,
                )
            ],
            max_concurrency=2,
        )
        runner = AgentRunner(coordinator, tools=background.tools)
        runner.subscribe(display)
        background.bind(runner)

        async with background:
            async with asyncio.timeout(10):
                await background.send("启动后台任务")
                await reader.started.wait()
                assert coordinator.task_id is not None
                assert background.get(coordinator.task_id).status == "running"

                # 子任务尚未结束，主 Agent 仍能回答并查询任务状态。
                await background.send("现在还能聊吗")
                await background.send("查询任务状态")
                assert coordinator.notifications == 0

                # 释放子任务后，无需再次调用主 Runner.run 或 continue_run。
                reader.release.set()
                await background.wait_until_idle()
                state = background.get(coordinator.task_id)
                assert state.status == "completed"
                assert state.notification_status == "delivered"
                assert coordinator.notifications == 1
                print(f"[任务状态] {state.status}，通知状态：{state.notification_status}")


if __name__ == "__main__":
    asyncio.run(main())
