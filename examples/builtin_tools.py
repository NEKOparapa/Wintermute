"""在临时目录中通过 AgentRunner 演示所有内置工具。"""

import asyncio
from collections.abc import AsyncIterator
from tempfile import TemporaryDirectory

from wintermute.agent import (
    AgentContext,
    AgentEvent,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    ToolCall,
    ToolResultMessage,
)
from wintermute.agent.tools import create_builtin_tools


class DemoModel:
    """使用输出固定的模拟模型，文件和命令工具均真实执行。"""

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        results = [m for m in context.messages if isinstance(m, ToolResultMessage)]
        if results and results[-1].is_error:
            raise RuntimeError(results[-1].content)
        steps = (
            ToolCall("write-note", "write", {"path": "demo/note.txt", "content": "Hello world!\n"}),
            ToolCall("read-note", "read", {"path": "demo/note.txt"}),
            ToolCall(
                "edit-note",
                "edit",
                {
                    "path": "demo/note.txt",
                    "edits": [{"old_text": "Hello world!", "new_text": "Hello Wintermute!"}],
                },
            ),
            ToolCall("run-command", "exec", {"command": "echo WINTERMUTE_TOOLS_OK"}),
            ToolCall("verify-note", "read", {"path": "demo/note.txt"}),
        )
        if len(results) < len(steps):
            yield AssistantMessage(tool_calls=(steps[len(results)],), finish_reason="tool_calls")
        else:
            assert results[-1].content.startswith("Hello Wintermute!")
            yield AssistantMessage("All four tools completed successfully.")


def display(event: AgentEvent) -> None:
    if event.type == "tool_execution_end" and event.tool_call and event.result:
        print(f"[{event.tool_call.name}] {event.result.content}")


async def main() -> None:
    with TemporaryDirectory(prefix="wintermute-tools-") as workspace:
        runner = AgentRunner(DemoModel(), tools=create_builtin_tools(workspace))
        runner.subscribe(display)
        result = await runner.run("Write, read and edit a note, then run a command and verify it.")
        print(f"{result.response.text} ({result.turns} model turns)")


if __name__ == "__main__":
    asyncio.run(main())
