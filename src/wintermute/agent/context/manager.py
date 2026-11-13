"""保留原始历史，以增量摘要和最近完整消息块构建模型上下文。"""

import asyncio
from bisect import bisect_right
from copy import deepcopy
from dataclasses import replace
from typing import Literal

from wintermute.agent.context.types import (
    ContextBudget,
    ContextCheckpoint,
    ContextLimitError,
    ContextState,
    EstimatedTokenCounter,
    ManagedContext,
    Summarizer,
    SummaryError,
    SummaryRequest,
    TokenCounter,
    history_digest,
)
from wintermute.agent.core.errors import AgentBusyError
from wintermute.agent.core.types import (
    AgentContext,
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
)

_SUMMARY_PREFIX = "[Conversation summary: historical context, not a new request]\n"
_SUMMARY_SUFFIX = "\n[/Conversation summary]"


def _boundaries(messages: tuple[Message, ...]) -> tuple[int, ...]:
    """返回可以安全切分的位置，工具调用和对应结果构成一个原子块。"""
    boundaries = [0]
    pending: dict[str, str] = {}
    for index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage):
            if pending.get(message.tool_call_id) != message.name:
                raise ValueError("History contains an unmatched tool result")
            del pending[message.tool_call_id]
        else:
            if pending:
                raise ValueError("History contains tool calls without results")
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if not call.id or not call.name or call.id in pending:
                        raise ValueError("Tool calls require nonempty, unique IDs and names")
                    pending[call.id] = call.name
            elif not isinstance(message, UserMessage):
                raise TypeError("History contains an unsupported message type")
        if not pending:
            boundaries.append(index + 1)
    if pending:
        raise ValueError("History ends with tool calls without results")
    return tuple(boundaries)


class SummaryContextManager:
    """使用可替换摘要器管理一个会话的模型输入。

    只有摘要、保留起点和前缀摘要值会缓存；原始消息一直由 Runner 保存。
    prepare 成功后才提交状态，失败或取消不会破坏已有摘要。计数预算以
    所注入计数器为准；默认估算不能代替模型服务端的实际上下文限制。
    """

    def __init__(
        self,
        budget: ContextBudget,
        summarizer: Summarizer,
        *,
        token_counter: TokenCounter | None = None,
        role: Literal["main", "subagent"] = "main",
    ) -> None:
        if role not in {"main", "subagent"}:
            raise ValueError("role must be main or subagent")
        self.budget = budget
        self.summarizer = summarizer
        self.token_counter = token_counter if token_counter is not None else EstimatedTokenCounter()
        self.role = role
        self._state = ContextState()
        self._prefix_digest: str | None = None
        self._busy = False

    @property
    def state(self) -> ContextState:
        return self._state

    @property
    def checkpoint(self) -> ContextCheckpoint | None:
        if self._prefix_digest is None:
            return None
        return ContextCheckpoint(
            self._state.summary,
            self._state.summarized_messages,
            self._prefix_digest,
            self._state.compactions,
        )

    def restore_checkpoint(self, checkpoint: ContextCheckpoint | None) -> None:
        """装入持久摘要；prepare 会核对实际历史前缀、工具边界和未读位置。"""
        self.reset()
        if checkpoint is not None:
            self._state = ContextState(
                checkpoint.summary, checkpoint.summarized_messages, checkpoint.compactions
            )
            self._prefix_digest = checkpoint.prefix_digest

    def reset(self) -> None:
        """空闲时清除摘要及缓存；下一次从完整输入历史重新准备。"""
        if self._busy:
            raise AgentBusyError("Context manager is preparing a request")
        self._state = ContextState()
        self._prefix_digest = None

    @staticmethod
    def _check_count(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Token counters must return nonnegative integers")
        return value

    def _count(self, context: AgentContext) -> int:
        return self._check_count(self.token_counter.count(context.for_model()))

    def _project(
        self, context: AgentContext, cut: int, summary: str, *, include_summary: bool = False
    ) -> AgentContext:
        messages = context.messages[cut:]
        if summary or include_summary:
            messages = (UserMessage(_SUMMARY_PREFIX + summary + _SUMMARY_SUFFIX), *messages)
        return replace(context, messages=messages)

    def _retention_cut(
        self, context: AgentContext, allowed_cuts: tuple[int, ...], previous_cut: int
    ) -> tuple[int, int]:
        """最多压缩到最早未读输入之前，并保持最后一组消息与工具回执完整。"""
        latest_start = allowed_cuts[-1]
        if latest_start == 0:
            raise ContextLimitError("Required original messages exceed the context budget")
        limit = self.budget.input_limit
        fixed = self._count(replace(context, messages=()))
        # 空摘要也保留标记开销，不能把消息挤满窗口后再追加摘要。
        for cut in allowed_cuts:
            if cut <= 0 or cut < previous_cut:
                continue
            tail = self._project(context, cut, "")
            tail_tokens = max(0, self._count(tail) - fixed)
            if tail_tokens > self.budget.keep_recent_tokens and cut != latest_start:
                continue
            available = limit - self._count(self._project(context, cut, "", include_summary=True))
            if available >= self.budget.max_summary_tokens:
                return cut, self.budget.max_summary_tokens
        # 固定提示词和最近原子块较大时，可缩小摘要输出额度，不能再删最近请求。
        available = limit - self._count(
            self._project(context, latest_start, "", include_summary=True)
        )
        if latest_start >= previous_cut and available > 0:
            return latest_start, min(available, self.budget.max_summary_tokens)
        raise ContextLimitError(
            "Fixed context and required original messages leave no room for a summary"
        )

    async def prepare(
        self, context: AgentContext, *, protected_from: int | None = None
    ) -> ManagedContext:
        if self._busy:
            raise AgentBusyError("Context manager cannot prepare concurrent requests")
        self._busy = True
        try:
            return await self._prepare(deepcopy(context), protected_from)
        finally:
            self._busy = False

    async def _prepare(self, context: AgentContext, protected_from: int | None) -> ManagedContext:
        boundaries = _boundaries(context.messages)
        if protected_from is not None and (
            type(protected_from) is not int or not 0 <= protected_from <= len(context.messages)
        ):
            raise ValueError("protected_from must be a valid history position")
        # 保留最后一个完整消息块；未读位置落在工具批次中时，也保留整批。
        allowed_cuts = boundaries[:-1] or (0,)
        if protected_from is not None:
            allowed_cuts = allowed_cuts[: bisect_right(allowed_cuts, protected_from)]
        fixed = self._count(replace(context, messages=()))
        if fixed > self.budget.input_limit:
            raise ContextLimitError("Fixed model input exceeds the context budget")

        previous_cut = self._state.summarized_messages
        summary = self._state.summary
        valid_cache = (
            previous_cut > 0
            and previous_cut in allowed_cuts
            and self._check_count(self.token_counter.count_text(summary))
            <= self.budget.max_summary_tokens
            and history_digest(context.messages[:previous_cut]) == self._prefix_digest
        )
        if not valid_cache:
            previous_cut, summary = 0, ""
        projection = self._project(context, previous_cut, summary)
        tokens = self._count(projection)
        cut = previous_cut
        compacted = False

        if tokens > self.budget.input_limit:
            cut, summary_limit = self._retention_cut(context, allowed_cuts, previous_cut)
            request = SummaryRequest(
                deepcopy(context.messages[previous_cut:cut]), summary, summary_limit, self.role
            )
            summary = await self.summarizer.summarize(request)
            if not isinstance(summary, str) or not summary.strip():
                raise SummaryError("Summarizer must return nonempty text")
            if self._check_count(self.token_counter.count_text(summary)) > summary_limit:
                raise SummaryError("Summary exceeds the requested token budget")
            projection = self._project(context, cut, summary)
            tokens = self._count(projection)
            if tokens > self.budget.input_limit:
                raise ContextLimitError(
                    "Summary and retained context still exceed the input budget"
                )
            compacted = True

        # 摘要器可能在返回前请求取消；接受取消后再决定是否提交，避免污染缓存。
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            await asyncio.sleep(0)
            raise asyncio.CancelledError
        digest = self._prefix_digest if valid_cache and cut == previous_cut else None
        if cut and digest is None:
            digest = history_digest(context.messages[:cut])
        result = ManagedContext(
            deepcopy(projection), tokens, self.budget.input_limit, compacted, cut
        )
        self._state = ContextState(
            summary,
            cut,
            self._state.compactions + int(compacted),
            tokens,
        )
        self._prefix_digest = digest
        return result


class MainContextManager(SummaryContextManager):
    """主 Agent 的上下文策略入口，可以独立扩展长期对话的管理方式。"""

    def __init__(
        self,
        budget: ContextBudget,
        summarizer: Summarizer,
        *,
        token_counter: TokenCounter | None = None,
    ) -> None:
        super().__init__(budget, summarizer, token_counter=token_counter, role="main")


class SubagentContextManager(SummaryContextManager):
    """子 Agent 的上下文策略入口；每个子任务应由工厂创建独立实例。"""

    def __init__(
        self,
        budget: ContextBudget,
        summarizer: Summarizer,
        *,
        token_counter: TokenCounter | None = None,
    ) -> None:
        super().__init__(budget, summarizer, token_counter=token_counter, role="subagent")
