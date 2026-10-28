"""上下文预算、状态与可替换管理策略的公共协议。"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from wintermute.agent.core.errors import AgentError
from wintermute.agent.core.types import AgentContext, Message


class ContextLimitError(AgentError):
    """必要上下文或摘要请求无法在所配置的预算内表示。"""


class SummaryError(AgentError):
    """摘要模型没有返回可用、完整且符合输出预算的摘要。"""


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """模型窗口与输出预留；最近原文和摘要预算均以计数器的单位计算。"""

    context_window: int
    reserve_tokens: int = 4096
    keep_recent_tokens: int = 8192
    max_summary_tokens: int = 2048

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("context_window", self.context_window, 1),
            ("reserve_tokens", self.reserve_tokens, 0),
            ("keep_recent_tokens", self.keep_recent_tokens, 0),
            ("max_summary_tokens", self.max_summary_tokens, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.reserve_tokens >= self.context_window:
            raise ValueError("reserve_tokens must be smaller than context_window")
        if self.keep_recent_tokens + self.max_summary_tokens >= self.input_limit:
            raise ValueError("Recent context and summary budgets must leave room for fixed context")

    @property
    def input_limit(self) -> int:
        return self.context_window - self.reserve_tokens


@dataclass(frozen=True, slots=True)
class ContextState:
    """最近一次成功准备后的摘要及压缩统计，不包含完整历史副本。"""

    summary: str = ""
    summarized_messages: int = 0
    compactions: int = 0
    last_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ManagedContext:
    """经过预算校验、可以发给模型的上下文投影。"""

    context: AgentContext
    estimated_tokens: int
    input_limit: int
    compacted: bool = False
    summarized_messages: int = 0


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """增量摘要请求；messages 仅包含上次摘要之后新移出的历史。"""

    messages: tuple[Message, ...]
    previous_summary: str
    max_tokens: int
    role: Literal["main", "subagent"]


class TokenCounter(Protocol):
    """为上下文和摘要正文计数；实现应使用相同的非负整数单位。"""

    def count(self, context: AgentContext) -> int: ...

    def count_text(self, text: str) -> int: ...


class EstimatedTokenCounter:
    """按 UTF-8 字节及协议开销保守估算，不等同于服务商的精确 token 数。

    系统提示词、完整工具声明以及 provider_data 均参与计算。普通文本按
    一个字节一个预算单位处理，可注入模型专用计数器减少保守估算的余量。
    """

    def count_text(self, text: str) -> int:
        return len(text.encode("utf-8"))

    def count(self, context: AgentContext) -> int:
        context = context.for_model()
        # 只计算最终发给模型的三部分，保持无临时资料时的既有估算单位。
        payload = json.dumps(
            {
                "system_prompt": context.system_prompt,
                "messages": [asdict(message) for message in context.messages],
                "tools": [asdict(tool) for tool in context.tools],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return self.count_text(payload) + 16


class Summarizer(Protocol):
    """将旧摘要和新增历史合并为一个完整摘要，失败或取消时抛出异常。"""

    async def summarize(self, request: SummaryRequest) -> str: ...


class ContextManager(Protocol):
    """每个会话持有独立实例；管理器可以替换压缩方式及上下文选择策略。

    返回时应保留 reference_messages；预算须按 context.for_model() 计数，
    动态资料不应进入历史摘要。未使用临时资料的既有管理器不受影响。
    """

    @property
    def state(self) -> ContextState: ...

    async def prepare(self, context: AgentContext) -> ManagedContext: ...

    def reset(self) -> None: ...


type ContextManagerFactory = Callable[[], ContextManager]
