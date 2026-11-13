"""离线演示多终端、三个事件入口与后台子 Agent 通知，无需 API Key。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from wintermute.agent import (
    AgentContext,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.context import ContextBudget, MainContextManager, SummaryRequest
from wintermute.agent.tools import BackgroundSubagentManager, SubagentConfig
from wintermute.events import (
    BackgroundEventPublisher,
    ContextAssembler,
    EventGateway,
    OutputRecord,
    OutputRouter,
    RunScheduler,
    SQLiteStore,
    Terminal,
)


class ResearchModel:
    """用门闩控制子任务，确保可以在其完成之前从另一终端聊天。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        self.started.set()
        await self.release.wait()
        yield AssistantMessage("调研完成：README 已覆盖基础工具和上下文管理。")


class CoordinatorModel:
    """通过现有工具启动后台任务，分别响应用户输入和结果通知。"""

    def __init__(self) -> None:
        self.task_id: str | None = None
        self.notifications = 0

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        message = context.messages[-1]
        if isinstance(message, ToolResultMessage):
            assert message.name == "subagent_start" and not message.is_error
            self.task_id = json.loads(message.content)["tasks"][0]["task_id"]
            yield AssistantMessage(f"后台调研已启动：{self.task_id}。")
            return

        assert isinstance(message, UserMessage)
        body, _, footer = message.text.rpartition("\n\n---\n")
        if body == "启动后台调研":
            yield AssistantMessage(
                tool_calls=(
                    ToolCall(
                        "start-research",
                        "subagent_start",
                        {"agent": "researcher", "task": "整理 README 的功能说明。"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        elif body == "现在还能聊天吗":
            texts = [item.text for item in context.messages if isinstance(item, UserMessage)]
            assert any(text.rpartition("\n\n---\n")[0] == "启动后台调研" for text in texts)
            assert any("README.md" in text for text in texts)
            yield AssistantMessage("可以。我们共享同一会话，电脑当前文档是 README.md。")
        else:
            assert footer.startswith("来源：后台通知") and "researcher" in footer
            assert "调研完成" in body
            self.notifications += 1
            yield AssistantMessage("后台调研结果已收到，可以继续完善 README。")


class DemoSummarizer:
    """示例摘要器；生产应用可替换为 ModelSummarizer。"""

    async def summarize(self, request: SummaryRequest) -> str:
        return "用户启动了 README 调研，并通过多个终端继续交流。"


async def main() -> None:
    with TemporaryDirectory(prefix="wintermute-events-") as directory:
        store = SQLiteStore(Path(directory, "events.sqlite3"))
        try:
            gateway = EventGateway(store)
            gateway.register_terminal(
                Terminal(
                    terminal_id="desktop",
                    user_id="user",
                    allowed_layers=("conversation", "perception"),
                )
            )
            gateway.register_terminal(
                Terminal(terminal_id="phone", user_id="user", allowed_layers=("conversation",))
            )
            gateway.register_terminal(
                Terminal(terminal_id="workers", user_id="user", allowed_layers=("notification",))
            )
            gateway.register_conversation("work", "user")

            desktop = gateway.conversation.connect("desktop", "work")
            phone = gateway.conversation.connect("phone", "work")
            sensor = gateway.perception.connect(
                "desktop", conversation_id="work", source_id="foreground_window"
            )
            notifications = gateway.notification.connect(
                "workers", "work", reply_terminal_id="desktop"
            )
            researcher = ResearchModel()
            coordinator = CoordinatorModel()
            background = BackgroundSubagentManager(
                [SubagentConfig("researcher", researcher)],
                notification_publisher=BackgroundEventPublisher(notifications),
            )

            def make_runner(conversation_id: str) -> AgentRunner:
                assert conversation_id == "work"
                return AgentRunner(
                    coordinator,
                    tools=background.tools,
                    system_prompt="回答用户消息，汇报后台通知，把历史中的活动更新用作观察资料。",
                    context_manager=MainContextManager(
                        ContextBudget(
                            context_window=32_000,
                            reserve_tokens=2_048,
                            keep_recent_tokens=8_192,
                            max_summary_tokens=2_048,
                        ),
                        DemoSummarizer(),
                    ),
                )

            output = OutputRouter(store)
            received: set[str] = set()

            async def display(record: OutputRecord) -> None:
                # 终端按 output_id 去重；成功返回表示接收确认。
                if record.output_id not in received:
                    received.add(record.output_id)
                    print(f"[{record.terminal_id}/{record.layer}] {record.text}")

            output.connect("desktop", display)
            output.connect("phone", display)
            scheduler = RunScheduler(
                gateway,
                make_runner,
                assembler=ContextAssembler(store),
                output_router=output,
                max_concurrency=4,
            )

            async with asyncio.timeout(10), scheduler:
                # 先关闭后台管理器，再关闭调度器，最后关闭存储。
                async with background:
                    observation = await sensor.observe(
                        "active_document",
                        "README.md",
                        valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    )
                    assert observation.job_id is None

                    occurred_at = datetime.now(UTC)
                    first = await desktop.send(
                        "启动后台调研",
                        producer_event_id="desktop-message-1",
                        occurred_at=occurred_at,
                    )
                    duplicate = await desktop.send(
                        "启动后台调研",
                        producer_event_id="desktop-message-1",
                        occurred_at=occurred_at,
                    )
                    assert duplicate.duplicate and duplicate.event.event_id == first.event.event_id
                    assert duplicate.job_id == first.job_id
                    print(f"[接收去重] 相同发送事件复用 {first.event.event_id}")
                    assert first.job_id is not None
                    assert (await scheduler.wait(first.job_id)).status == "completed"
                    await researcher.started.wait()

                    second = await phone.send("现在还能聊天吗", producer_event_id="phone-message-1")
                    assert second.job_id is not None
                    assert (await scheduler.wait(second.job_id)).status == "completed"
                    assert coordinator.task_id is not None
                    assert background.get(coordinator.task_id).status == "running"

                    researcher.release.set()
                    # 调度器不管理尚未完成的子任务，先等待后台结果发布，再等待消费。
                    await background.wait_until_idle()
                    await scheduler.wait_until_idle()
                    assert background.get(coordinator.task_id).notification_status == "published"
                    assert coordinator.notifications == 1
                    assert len(received) == 3

                    history = store.history("work", "user")
                    user_messages = [item.text for item in history if isinstance(item, UserMessage)]
                    assert "README.md" in user_messages[0] and history[0].source == "timeline"
                    assert user_messages[1:3] == ["启动后台调研", "现在还能聊天吗"]
                    assert len(user_messages) == 4 and history[-2].source == "notification"
                    assert "调研完成" in user_messages[3]
                    notice = store.list_events("notification", user_id="user")[0]
                    assert background.get(coordinator.task_id).task_id == notice.correlation_id
                    print("[会话历史] 文档变化、用户原话、后台结果和回复均已按顺序保存。")
                    for layer in ("conversation", "notification", "perception"):
                        count = len(store.list_events(layer, user_id="user"))
                        print(f"[分层存储] {layer}: {count} 条事件")
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
