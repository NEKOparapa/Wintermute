from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..event.event import StandardEvent
from ..memory.consolidator import Consolidator
from ..memory.memory import MemoryStore
from ..memory.tokens import TokenCounter
from ..prompt.prompt import (
    DEFAULT_BUDGET_DAILY,
    DEFAULT_BUDGET_MONTHLY,
    DEFAULT_BUDGET_SESSION,
    DEFAULT_BUDGET_WEEKLY,
    DEFAULT_RECENT_ROUNDS,
    build_messages,
)
from ..storage.storage import GlobalEventStore
from ..translation.translation import AIResponseType, assistant_event_type, translate_ai_response

logger = logging.getLogger(__name__)

# session 压缩触发阈值。当"最近 N 轮之前的未压缩事件"加起来超过这个 token 数,
# build_messages 之前先把它们压成一条 session 记忆,避免历史信息被截断丢失。
DEFAULT_SESSION_COMPRESS_TRIGGER_TOKENS = 4000


@dataclass
class TurnResult:
    """一次对话处理后的返回结果。"""

    message: str
    response_type: AIResponseType


class DialogueService:
    """对话流程服务,负责处理 L0 用户消息事件。"""

    def __init__(
        self,
        store: GlobalEventStore,
        llm,
        memory_store: MemoryStore,
        consolidator: Consolidator,
        token_counter: TokenCounter,
        *,
        recent_rounds: int = DEFAULT_RECENT_ROUNDS,
        session_compress_trigger_tokens: int = DEFAULT_SESSION_COMPRESS_TRIGGER_TOKENS,
        budget_session: int = DEFAULT_BUDGET_SESSION,
        budget_daily: int = DEFAULT_BUDGET_DAILY,
        budget_weekly: int = DEFAULT_BUDGET_WEEKLY,
        budget_monthly: int = DEFAULT_BUDGET_MONTHLY,
    ) -> None:
        """注入存储、LLM、记忆相关组件,以及窗口/阈值/预算等可调参数。"""
        self.store = store
        self.llm = llm
        self.memory_store = memory_store
        self.consolidator = consolidator
        self.token_counter = token_counter
        self.recent_rounds = recent_rounds
        self.session_compress_trigger_tokens = session_compress_trigger_tokens
        self.budget_session = budget_session
        self.budget_daily = budget_daily
        self.budget_weekly = budget_weekly
        self.budget_monthly = budget_monthly

    def handle_event(self, event: StandardEvent) -> TurnResult:
        """处理一条 L0 用户消息事件,并返回助手回复。"""
        if event.type != "user_message":
            raise ValueError(f"暂不支持的事件类型: {event.type}")

        logger.info("L0 对话事件处理开始 length=%s", len(event.content))

        # 先保存标准事件,后续 prompt 只从事件历史构造,避免重复追加当前输入。
        self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
        )

        # 必要时把"最近 N 轮之前"的未压缩事件压成一条 session 记忆,
        # 失败时只记日志,不影响当前轮对话。
        self._maybe_compress_session()

        prompt = build_messages(
            events=self.store.load_events(),
            memory_store=self.memory_store,
            recent_rounds=self.recent_rounds,
            budget_session=self.budget_session,
            budget_daily=self.budget_daily,
            budget_weekly=self.budget_weekly,
            budget_monthly=self.budget_monthly,
        )
        response = self.llm.complete(system=prompt.system, messages=prompt.messages)
        translated = translate_ai_response(response)

        self.store.append_event(
            source="assistant",
            type=assistant_event_type(translated.response_type),
            content=translated.raw_response,
            metadata={"response_type": translated.response_type.value},
        )
        logger.info(
            "L0 对话事件处理完成 response_type=%s response_length=%s",
            translated.response_type.value,
            len(translated.content),
        )
        return TurnResult(
            message=translated.content,
            response_type=translated.response_type,
        )

    # ----------------------------------------------------------------- 压缩入口

    def _maybe_compress_session(self) -> None:
        """检查今日未压缩事件中"最近 N 轮之前"的部分,超阈值就压成 session 记忆。"""
        today_events = self.store.load_events_for_date(date.today())
        compressed_ids = self.memory_store.compressed_event_ids()
        candidates = [
            event for event in today_events if str(event.get("id")) not in compressed_ids
        ]

        to_compress, _ = _split_by_recent_rounds(candidates, self.recent_rounds)
        if not to_compress:
            return

        token_estimate = self._estimate_event_tokens(to_compress)
        if token_estimate < self.session_compress_trigger_tokens:
            return

        period_start = _event_timestamp(to_compress[0])
        period_end = _event_timestamp(to_compress[-1])

        try:
            entry = self.consolidator.compress_to_session(
                to_compress,
                period_start=period_start,
                period_end=period_end,
            )
        except Exception:
            # 压缩失败不能阻塞对话;事件会留在原文件等下次再压。
            logger.exception(
                "session 压缩失败 events=%d tokens=%d", len(to_compress), token_estimate
            )
            return

        self.memory_store.append(entry)
        logger.info(
            "session 压缩完成 events=%d source_tokens=%d summary_tokens=%d",
            len(to_compress),
            token_estimate,
            entry.tokens,
        )

    def _estimate_event_tokens(self, events: list[dict[str, Any]]) -> int:
        """估算一批事件转成 chat 消息后的 token 数,用于触发判定。"""
        messages = [
            {"role": _role(event), "content": str(event.get("content", ""))}
            for event in events
            if _role(event) is not None
        ]
        return self.token_counter.count_messages(messages)


# ============================================================== 模块级辅助


def _split_by_recent_rounds(
    events: list[dict[str, Any]],
    rounds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把事件列表切成 (要压缩的早期部分, 要保留的最近 N 轮事件)。

    切分点是从尾部往前数第 N 个 ``user_message`` 事件;不足 N 轮时第一段为空。
    """
    if rounds <= 0 or not events:
        return list(events), []

    user_seen = 0
    cutoff = 0
    for index in range(len(events) - 1, -1, -1):
        if events[index].get("type") == "user_message":
            user_seen += 1
            if user_seen == rounds:
                cutoff = index
                break
    else:
        cutoff = 0
    return events[:cutoff], events[cutoff:]


def _role(event: dict[str, Any]) -> str | None:
    """把事件类型转成 chat 角色,用不上的类型返回 None。"""
    event_type = event.get("type")
    if event_type == "user_message":
        return "user"
    if event_type in {
        "assistant_response",
        "assistant_natural_response",
        "assistant_question",
    }:
        return "assistant"
    return None


def _event_timestamp(event: dict[str, Any]) -> datetime:
    """安全地把事件的 ISO timestamp 解析成 datetime,缺失时回退到当前时间。"""
    raw = event.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now().astimezone()
