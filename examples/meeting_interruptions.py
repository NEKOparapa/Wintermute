"""用真实的调度器、工具和 SQLite 重现会议中的引导与通知。模型完全离线。"""

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from wintermute.agent import (
    AgentContext,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.tools import (
    BackgroundSubagentManager,
    ReadInput,
    SubagentConfig,
    create_read_tool,
    create_write_tool,
)
from wintermute.events import (
    BackgroundEventPublisher,
    EventGateway,
    RunScheduler,
    SQLiteStore,
    Terminal,
)

type Stage = Literal["生成首字前", "生成中", "读取清单时"]

QUESTION = "把刚才会上定的事列出来，再核对我正在看的演示清单，告诉我漏了哪一项。"
GUIDANCE = "只告诉我漏了哪一项，先别改清单。"
CHECKLIST = "docs/demo-checklist.md"
AGENDA = "docs/meeting-agenda.md"
MEETING = "演示现场展示 ESP32 录音上传，并在演示前验证断网重试。"
SECOND_SLICE = "会上约定由小林负责演示，周五下午彩排。"
DURING_READ = "清单核对期间，会议又确认录音上传已列入清单。"
CHECKLIST_TEXT = "- 现场展示 ESP32 录音上传\n"
PARTIAL = "会上首先决定展示"
FINDING = "reader 核对结果：清单上有录音上传演示，缺少断网重试验证。"
FINAL = "漏了一项：演示前验证断网重试。"


class Reader:
    """实际读取清单，控制何时把核对结果返回后台任务管理器。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        last = context.messages[-1]
        if isinstance(last, ToolResultMessage):
            assert last.name == "read" and not last.is_error
            assert "现场展示 ESP32 录音上传" in last.content and "断网重试" not in last.content
            await self.release.wait()
            yield AssistantMessage(FINDING)
        else:
            assert isinstance(last, UserMessage) and CHECKLIST in last.text and MEETING in last.text
            yield AssistantMessage(
                tool_calls=(ToolCall("child-read", "read", {"path": CHECKLIST}),)
            )


class MeetingModel:
    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.awaiting_result = asyncio.Event()
        self.requests: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        self.requests.append(context)
        if len(self.requests) == 1:
            yield AssistantMessage(
                tool_calls=(
                    ToolCall(
                        "delegate",
                        "subagent_start",
                        {
                            "agent": "reader",
                            "task": f"读取 {CHECKLIST}，与以下会议决定核对。{MEETING}",
                        },
                    ),
                )
            )
        elif len(self.requests) == 2 and self.stage == "读取清单时":
            yield AssistantMessage(
                tool_calls=(
                    ToolCall("read-checklist", "read", {"path": CHECKLIST}),
                    ToolCall(
                        "rewrite-checklist", "write", {"path": CHECKLIST, "content": "旧计划\n"}
                    ),
                )
            )
        elif len(self.requests) == 2:
            try:
                if self.stage == "生成中":
                    yield TextDelta(PARTIAL)
                self.entered.set()
                # 未被打断则永远没有下一个 token，也没有工具或最终消息。
                await asyncio.Event().wait()
            finally:
                self.closed.set()
        else:
            inputs = [read_event(m) for m in context.messages if isinstance(m, UserMessage)]
            received = {(origin, body) for body, origin, _ in inputs}
            guided = ("用户原话 · telegram", GUIDANCE) in received
            if guided and ("后台通知 · worker · reader", FINDING) in received:
                yield AssistantMessage(FINAL)
            else:
                # 两条输入不必同时到达。等待后续输入打断这次推理。
                if guided:
                    self.awaiting_result.set()
                await asyncio.Event().wait()


def print_model_requests(requests: list[AgentContext]) -> None:
    print("模型为离线 MeetingModel。以下取自主 Agent 传给 model.stream 的 AgentContext。")
    print("每轮都展开完整消息。工具定义在 JSON 导出中保留。")
    for index, request in enumerate(requests, start=1):
        print(f"\n主模型请求 {index}，共 {len(request.messages)} 条消息")
        if index == 1:
            print(f"  system  {request.system_prompt or '（空）'}")
            print("  tools   " + "、".join(tool.name for tool in request.tools))
        for number, message in enumerate(request.messages, start=1):
            print(f"  {number:02d} {message.role}")
            if isinstance(message, ToolResultMessage):
                print(
                    f"     {message.name} (call_id={message.tool_call_id}, "
                    f"is_error={message.is_error})"
                )
                print(message.content)
            else:
                if message.text:
                    print(message.text)
                if isinstance(message, AssistantMessage):
                    for call in message.tool_calls:
                        arguments = json.dumps(call.arguments, ensure_ascii=False)
                        print(f"     {call.name} (call_id={call.id}) {arguments}")


def read_event(message: UserMessage) -> tuple[str, str, str]:
    body, separator, footer = message.text.rpartition("\n\n---\n")
    origin, newline, timestamp = footer.partition("\n")
    assert separator and newline and origin.startswith("来源：") and timestamp.startswith("时间：")
    return body, origin.removeprefix("来源："), timestamp.removeprefix("时间：")


def check_timeline(message: UserMessage, expected: list[tuple[str, str]]) -> None:
    body, origin, _ = read_event(message)
    assert origin == "活动时间线"
    entries = [line.split(" | ", 2) for line in body.splitlines()]
    assert [(source, text) for _, source, text in entries] == expected
    times = [datetime.fromisoformat(timestamp) for timestamp, _, _ in entries]
    assert times == sorted(times) and all(time.utcoffset() == UTC.utcoffset(None) for time in times)


def write_requests(path: Path, stage: Stage, requests: list[AgentContext]) -> None:
    records = []
    for index, request in enumerate(requests, start=1):
        record = asdict(request)
        # stream 的用户消息已经投影：导出 text 原文，省略空的 Python 对象字段。
        record["messages"] = [
            {"role": message.role, "text": message.text}
            if isinstance(message, UserMessage)
            else asdict(message)
            for message in request.messages
        ]
        records.append({"request_index": index, **record})
    captured = {
        "model": "MeetingModel (offline)",
        "boundary": "AgentContext passed to the main model's stream method",
        "stage": stage,
        "requests": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def run_meeting(
    directory: Path, stage: Stage, *, show_timeline: bool = False, dump_requests: Path | None = None
) -> str:
    database = directory / "meeting.sqlite3"
    checklist = directory / CHECKLIST
    checklist.parent.mkdir(parents=True)
    checklist.write_text(CHECKLIST_TEXT, encoding="utf-8")
    read = create_read_tool(directory)
    read_release = asyncio.Event()
    model = MeetingModel(stage)
    reader = Reader()

    async def gated_read(params: ReadInput, context: ToolContext) -> ToolResult:
        model.entered.set()
        await read_release.wait()
        return await read.execute(params, context)

    async with asyncio.timeout(10):
        with SQLiteStore(database) as store:
            gateway = EventGateway(store)
            for terminal, layer in (
                ("telegram", "conversation"),
                ("worker", "notification"),
                ("desktop", "perception"),
                ("esp32", "slice"),
            ):
                gateway.register_terminal(
                    Terminal(terminal_id=terminal, user_id="owner", allowed_layers=(layer,))
                )
            gateway.register_conversation("work", "owner")
            chat = gateway.conversation.connect("telegram", "work")
            notices = gateway.notification.connect(
                "worker", "work", reply_terminal_id="telegram", source_id="background_subagent"
            )
            background = BackgroundSubagentManager(
                [SubagentConfig("reader", reader, tools=(read,))],
                notification_publisher=BackgroundEventPublisher(notices),
            )
            tools = (
                *background.tools,
                replace(read, execute=gated_read),
                create_write_tool(directory),
            )
            scheduler = RunScheduler(gateway, lambda _: AgentRunner(model, tools=tools))
            events: list[str] = []
            scheduler.subscribe(lambda event: events.append(event.event.type))
            async with scheduler, background:
                slices = gateway.slice.connect("esp32", conversation_id="work")
                screen = gateway.perception.connect("desktop", conversation_id="work")
                first_screen = await screen.observe("active_document", CHECKLIST)
                first_slice = await slices.record(MEETING)
                await scheduler.wait_for_slices()
                await screen.observe("active_document", CHECKLIST)
                second_slice = await slices.record(SECOND_SLICE)
                await scheduler.wait_for_slices()
                changed = await screen.observe("active_document", AGENDA)
                restored = await screen.observe("active_document", CHECKLIST)
                first = await chat.send(QUESTION, producer_event_id="question")
                await model.entered.wait()
                if stage == "读取清单时":
                    await screen.observe("active_document", CHECKLIST)
                    connectivity = await screen.observe("network", "正在测试断网重试")
                    during_read = await slices.record(DURING_READ)
                    await scheduler.wait_for_slices()
                guide = await chat.send(GUIDANCE, producer_event_id="steer")
                duplicate = await chat.send(GUIDANCE, producer_event_id="steer")
                assert duplicate.duplicate and duplicate.job_id == guide.job_id
                if stage != "读取清单时":
                    await model.closed.wait()
                    await model.awaiting_result.wait()
                reader.release.set()
                await background.wait_until_idle()
                assert len(store.list_events("notification", user_id="owner")) == 1
                read_release.set()
                assert first.job_id and guide.job_id
                assert (await scheduler.wait(first.job_id)).status == "completed"
                assert (await scheduler.wait(guide.job_id)).status == "completed"
                await scheduler.wait_until_idle()
                assert events.count("agent_start") == 1

    assert checklist.read_text(encoding="utf-8") == CHECKLIST_TEXT
    if show_timeline:
        print_model_requests(model.requests)
    if dump_requests is not None:
        await asyncio.to_thread(write_requests, dump_requests, stage, model.requests)
    initial_messages = model.requests[0].messages
    user_origin = "用户原话 · telegram"
    screen_origin = "感知 · desktop"
    slice_origin = "切片 · esp32"
    notice_origin = "后台通知 · worker · reader"
    assert len(initial_messages) == 2 and all(isinstance(m, UserMessage) for m in initial_messages)
    timeline, question = initial_messages
    check_timeline(
        timeline,
        [
            (screen_origin, f"active_document：{CHECKLIST}"),
            (slice_origin, MEETING),
            (slice_origin, SECOND_SLICE),
            (screen_origin, f"active_document：{AGENDA}"),
            (screen_origin, f"active_document：{CHECKLIST}"),
        ],
    )
    assert read_event(question)[:2] == (QUESTION, user_origin)
    final_messages = model.requests[-1].messages
    incoming = [read_event(m) for m in final_messages if isinstance(m, UserMessage)]
    assert [(body, origin) for body, origin, _ in incoming[-2:]] == [
        (GUIDANCE, user_origin),
        (FINDING, notice_origin),
    ]
    with SQLiteStore(database) as reopened:
        history = reopened.history("work", "owner")
        assert [m.role for m in history[:-1]] == [m.role for m in final_messages]
        persisted = [m for m in history if isinstance(m, UserMessage)]
        user_texts = [m.text for m in persisted]
        assert [body for body, _, _ in incoming] == user_texts
        assert user_texts.count(QUESTION) == user_texts.count(GUIDANCE) == 1
        (notice,) = reopened.list_events("notification", user_id="owner")
        assert notice.type == "subagent.completed" and notice.payload["output"] == FINDING
        assert datetime.fromisoformat(read_event(question)[2]) == first.event.occurred_at
        assert datetime.fromisoformat(incoming[-2][2]) == guide.event.occurred_at
        assert datetime.fromisoformat(incoming[-1][2]) == notice.occurred_at
        audit = reopened.list_activity(user_id="owner", conversation_id="work")
        source_receipts = [first_screen, first_slice, second_slice, changed, restored]
        if stage == "读取清单时":
            source_receipts.extend([connectivity, during_read])
        assert [item.source_event_ids for item in audit] == [
            (receipt.event.event_id,) for receipt in source_receipts
        ]
        task_receipt = next(
            m
            for m in final_messages
            if isinstance(m, ToolResultMessage) and m.tool_call_id == "delegate"
        )
        handle = json.loads(task_receipt.content)["tasks"][0]["task_id"]
        task = background.get(handle)
        assert task.status == "completed" and task.output == FINDING
        assert notice.correlation_id == task.task_id == notice.payload["task_id"]
        assert handle != task.task_id
        audit_ids = {task.task_id, *(item.activity_id for item in audit)}
        for layer in ("conversation", "perception", "slice", "notification"):
            audit_ids.update(
                event.event_id for event in reopened.list_events(layer, user_id="owner")
            )
        audit_ids.update(job.job_id for job in reopened.list_jobs(user_id="owner"))
        audit_ids.update(output.output_id for output in reopened.list_outputs(user_id="owner"))
        for request in model.requests:
            for message in request.messages:
                serialized = json.dumps(asdict(message), ensure_ascii=False)
                assert not any(identifier in serialized for identifier in audit_ids)
        assert history[-1] == AssistantMessage(FINAL)
        guide_message = next(m for m in persisted if m.text == GUIDANCE)
        if stage == "读取清单时":
            results = {m.tool_call_id: m for m in history if isinstance(m, ToolResultMessage)}
            assert CHECKLIST_TEXT.strip() in results["read-checklist"].content
            assert results["rewrite-checklist"].is_error
            declaration = next(
                m
                for m in history
                if isinstance(m, AssistantMessage)
                and m.tool_calls
                and m.tool_calls[0].id == "read-checklist"
            )
            index = history.index(declaration)
            assert history[index + 1 : index + 3] == (
                results["read-checklist"],
                results["rewrite-checklist"],
            )
            during = history[index + 3]
            assert isinstance(during, UserMessage) and during.source == "timeline"
            check_timeline(
                final_messages[-3],
                [
                    (screen_origin, "network：正在测试断网重试"),
                    (slice_origin, DURING_READ),
                ],
            )
            assert history[index + 4] == guide_message
        assert (
            sum(
                isinstance(m, AssistantMessage) and m.text == "" and not m.tool_calls
                for m in history
            )
            == 0
        )
        fragments = [m for m in history if isinstance(m, AssistantMessage) and PARTIAL in m.text]
        assert len(fragments) == (1 if stage == "生成中" else 0)
        if fragments:
            assert history.index(fragments[0]) < history.index(guide_message)
        outputs = reopened.list_outputs(user_id="owner", terminal_id="telegram")
        assert len(outputs) == 1 and outputs[0].text == FINAL
        for job in reopened.list_jobs(user_id="owner"):
            assert job.status == "completed"
            assert reopened.list_outputs(job_id=job.job_id, terminal_id="telegram") == outputs
        assert len(reopened.list_jobs(user_id="owner")) == 3
        return json.dumps(
            {
                "时机": stage,
                "主 Agent 运行": events.count("agent_start"),
                "提问前的时间线消息": 1,
                "提问前合并的活动": len(timeline.text.partition("\n\n---\n")[0].splitlines()),
                "插入运行中的时间线消息": sum(
                    isinstance(m, UserMessage) and m.source == "timeline"
                    for m in history[len(initial_messages) :]
                ),
                "用户引导次数": user_texts.count(GUIDANCE),
                "后台结果次数": user_texts.count(FINDING),
                "被新输入打断的模型请求": events.count("turn_interrupted"),
                "保存的已输出片段": len(fragments),
                "拦住的写入": stage == "读取清单时",
                "清单内容": checklist.read_text(encoding="utf-8").strip(),
                "回复终端": outputs[0].terminal_id,
                "最终回复": outputs[0].text,
            },
            ensure_ascii=False,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeline", action="store_true", help="只运行工具阶段，展开主模型实际收到的消息"
    )
    parser.add_argument(
        "--dump-requests",
        type=Path,
        metavar="PATH",
        help="配合 --timeline，将每次完整模型请求写为 UTF-8 JSON",
    )
    options = parser.parse_args()
    if options.dump_requests is not None and not options.timeline:
        parser.error("--dump-requests 需要同时指定 --timeline")
    for stage in ("读取清单时",) if options.timeline else ("生成首字前", "生成中", "读取清单时"):
        with TemporaryDirectory(prefix="wintermute-meeting-") as directory:
            print(
                await run_meeting(
                    Path(directory),
                    stage,
                    show_timeline=options.timeline,
                    dump_requests=options.dump_requests,
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
