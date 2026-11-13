"""持久摘要恢复和未消费输入的原文边界。"""

from dataclasses import asdict, replace

import pytest

from wintermute.agent import (
    AgentContext,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.context import (
    ContextBudget,
    ContextCheckpoint,
    ContextLimitError,
    MainContextManager,
    SummaryRequest,
)


class Summaries:
    def __init__(self) -> None:
        self.calls: list[SummaryRequest] = []

    async def summarize(self, request: SummaryRequest) -> str:
        self.calls.append(request)
        return "用户请核对会议清单。"


def manager(summaries: Summaries) -> MainContextManager:
    return MainContextManager(
        ContextBudget(2300, reserve_tokens=100, keep_recent_tokens=0, max_summary_tokens=180),
        summaries,
    )


OLD = (UserMessage("旧历史" * 900), AssistantMessage("已核对"))
UNREAD = (
    UserMessage("正在读清单", "timeline", "09:00", "活动时间线"),
    UserMessage("先别改", "user", "09:01", "用户原话 · 手机"),
    UserMessage("漏了断网验证", "notification", "09:02", "后台通知 · reader"),
)


async def test_checkpoint_round_trip_then_incremental_compaction_keeps_unread_input() -> None:
    first_calls = Summaries()
    initial = manager(first_calls)
    history = AgentContext("", OLD + UNREAD, ())
    prepared = await initial.prepare(history, protected_from=len(OLD))
    assert first_calls.calls[0].messages == OLD
    assert prepared.context.messages[1:] == UNREAD
    checkpoint = initial.checkpoint
    assert checkpoint and checkpoint.summarized_messages == len(OLD)
    restored_calls = Summaries()
    restored = manager(restored_calls)
    restored.restore_checkpoint(ContextCheckpoint(**asdict(checkpoint)))
    resumed = await restored.prepare(history, protected_from=len(OLD))
    assert resumed.context == prepared.context and not restored_calls.calls
    extension = UNREAD + (AssistantMessage("核对过程" * 650),)
    latest = UserMessage("下一件事", "user", "09:30", "用户原话 · 手机")
    longer = replace(history, messages=OLD + extension + (latest,))
    await restored.prepare(longer, protected_from=len(longer.messages) - 1)
    assert restored_calls.calls[0].messages == extension
    assert restored_calls.calls[0].previous_summary == checkpoint.summary
    assert restored.checkpoint.compactions == checkpoint.compactions + 1


async def test_checkpoint_does_not_hide_unread_input_or_mismatched_prefix() -> None:
    summaries = Summaries()
    current = manager(summaries)
    history = AgentContext("", OLD + UNREAD, ())
    await current.prepare(history)
    checkpoint = current.checkpoint
    assert checkpoint.summarized_messages > len(OLD)
    # 即使摘要和摘要指纹都有效，也不能跨越未读位置。
    await current.prepare(history, protected_from=len(OLD))
    assert summaries.calls[-1].previous_summary == ""
    assert summaries.calls[-1].messages == OLD
    rewritten = replace(history, messages=(UserMessage("更正" * 800), *history.messages[1:]))
    current.restore_checkpoint(checkpoint)
    await current.prepare(rewritten)
    assert summaries.calls[-1].previous_summary == ""
    assert summaries.calls[-1].messages[0] == rewritten.messages[0]


async def test_protection_rounds_to_tool_batch_and_rejects_oversize_unread_tail() -> None:
    summaries = Summaries()
    current = manager(summaries)
    batch = (
        AssistantMessage(tool_calls=(ToolCall("read", "read", {}), ToolCall("write", "write", {}))),
        ToolResultMessage("read", "read", "已读"),
        ToolResultMessage("write", "write", "跳过", True),
    )
    history = AgentContext("", OLD + batch + UNREAD, ())
    prepared = await current.prepare(history, protected_from=len(OLD) + 1)
    assert prepared.context.messages[1:] == batch + UNREAD
    previous = current.checkpoint
    too_big = replace(history, messages=history.messages + (UserMessage("长原文" * 900),))
    with pytest.raises(ContextLimitError, match="required original messages"):
        await current.prepare(too_big, protected_from=len(OLD))
    assert current.checkpoint == previous
    assert len(summaries.calls) == 1 and summaries.calls[0].messages == OLD
