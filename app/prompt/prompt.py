from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..memory.memory import MemoryEntry, MemoryKind, MemoryStore

# ===== 默认参数。可被 build_messages 调用方覆盖；slice 4 会接到 config。 =====

DEFAULT_RECENT_ROUNDS = 5
DEFAULT_BUDGET_SESSION = 8000
DEFAULT_BUDGET_DAILY = 8000
DEFAULT_BUDGET_WEEKLY = 4000
DEFAULT_BUDGET_MONTHLY = 2000

SYSTEM_PROMPT_BASE = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。
"""


@dataclass(frozen=True)
class PromptContent:
    """发送给 LLM 前的提示内容,系统提示词和历史消息分开保存。"""

    system: str
    messages: list[dict[str, str]]


def build_messages(
    *,
    events: list[dict[str, Any]],
    memory_store: MemoryStore,
    recent_rounds: int = DEFAULT_RECENT_ROUNDS,
    budget_session: int = DEFAULT_BUDGET_SESSION,
    budget_daily: int = DEFAULT_BUDGET_DAILY,
    budget_weekly: int = DEFAULT_BUDGET_WEEKLY,
    budget_monthly: int = DEFAULT_BUDGET_MONTHLY,
) -> PromptContent:
    """构造发送给 LLM 的系统提示词和短期消息。

    系统提示词中注入按层级筛选后的长期记忆;消息列表只保留最近 ``recent_rounds``
    轮且未被 session 记忆收录的原始事件。
    """
    compressed_ids = memory_store.compressed_event_ids()

    # 1. 过滤已经被压缩的原始事件,然后转成 chat 消息。
    chat_messages: list[dict[str, str]] = []
    for event in events:
        if event.get("id") in compressed_ids:
            continue
        message = _event_to_message(event)
        if message is not None:
            chat_messages.append(message)

    # 2. 截断到最近 N 轮(轮 = 一条 user 消息 + 其后所有 assistant 消息)。
    recent_messages = _take_last_rounds(chat_messages, recent_rounds)

    # 3. 选出每一层要注入的记忆条目,且层之间 period 互不覆盖。
    selected = _select_memories(
        memory_store=memory_store,
        budgets={
            MemoryKind.MONTHLY: budget_monthly,
            MemoryKind.WEEKLY: budget_weekly,
            MemoryKind.DAILY: budget_daily,
            MemoryKind.SESSION: budget_session,
        },
    )

    # 4. 拼装最终 system prompt。
    system = SYSTEM_PROMPT_BASE
    memory_block = _format_memory_block(selected)
    if memory_block:
        system = system.rstrip() + "\n\n" + memory_block

    return PromptContent(system=system, messages=recent_messages)


# ============================================================== 事件 → 消息


_USER_EVENT_TYPES = {"user_message"}
_ASSISTANT_EVENT_TYPES = {
    "assistant_response",
    "assistant_natural_response",
    "assistant_question",
}


def _event_to_message(event: dict[str, Any]) -> dict[str, str] | None:
    """把单个事件转换成 chat 消息;无法识别的类型返回 None 直接跳过。"""
    event_type = event.get("type")
    content = str(event.get("content", ""))
    if event_type in _USER_EVENT_TYPES:
        return {"role": "user", "content": content}
    if event_type in _ASSISTANT_EVENT_TYPES:
        return {"role": "assistant", "content": content}
    return None


def _take_last_rounds(
    messages: list[dict[str, str]],
    rounds: int,
) -> list[dict[str, str]]:
    """从消息列表尾部回溯,保留最近 rounds 个 user 消息及其后的所有内容。"""
    if rounds <= 0 or not messages:
        return []

    user_seen = 0
    cutoff = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index]["role"] == "user":
            user_seen += 1
            if user_seen == rounds:
                cutoff = index
                break
    else:
        # 没攒够 rounds 个 user 消息,就把全部消息都返回。
        cutoff = 0
    return messages[cutoff:]


# ============================================================== 记忆筛选


def _select_memories(
    *,
    memory_store: MemoryStore,
    budgets: dict[MemoryKind, int],
) -> dict[MemoryKind, list[MemoryEntry]]:
    """返回每层注入到 prompt 的记忆条目;高层记忆覆盖的 period 会从低层剔除。"""
    monthly = memory_store.load_by_kind(MemoryKind.MONTHLY)
    weekly = memory_store.load_by_kind(MemoryKind.WEEKLY)
    daily = memory_store.load_by_kind(MemoryKind.DAILY)
    session = memory_store.load_by_kind(MemoryKind.SESSION)

    monthly_ranges = _periods(monthly)
    weekly = _exclude_covered(weekly, monthly_ranges)
    weekly_ranges = _periods(weekly)
    daily = _exclude_covered(daily, monthly_ranges + weekly_ranges)
    daily_ranges = _periods(daily)
    session = _exclude_covered(session, monthly_ranges + weekly_ranges + daily_ranges)

    return {
        MemoryKind.MONTHLY: _fit_budget(monthly, budgets[MemoryKind.MONTHLY]),
        MemoryKind.WEEKLY: _fit_budget(weekly, budgets[MemoryKind.WEEKLY]),
        MemoryKind.DAILY: _fit_budget(daily, budgets[MemoryKind.DAILY]),
        MemoryKind.SESSION: _fit_budget(session, budgets[MemoryKind.SESSION]),
    }


def _periods(entries: list[MemoryEntry]) -> list[tuple[datetime, datetime]]:
    """把记忆列表展开成 (start, end) 的 period 范围列表。"""
    return [(entry.period_start, entry.period_end) for entry in entries]


def _exclude_covered(
    entries: list[MemoryEntry],
    higher_ranges: list[tuple[datetime, datetime]],
) -> list[MemoryEntry]:
    """把 period 完全落在更高层级范围内的条目剔除掉。"""
    if not higher_ranges:
        return list(entries)
    return [
        entry
        for entry in entries
        if not any(
            start <= entry.period_start and entry.period_end <= end
            for start, end in higher_ranges
        )
    ]


def _fit_budget(
    entries: list[MemoryEntry],
    budget_tokens: int,
) -> list[MemoryEntry]:
    """从最近的条目向回收集,直到 token 预算用完;最后再按时间顺序返回。"""
    if budget_tokens <= 0 or not entries:
        return []
    selected: list[MemoryEntry] = []
    used = 0
    for entry in reversed(entries):
        cost = max(int(entry.tokens), 0)
        if selected and used + cost > budget_tokens:
            break
        selected.append(entry)
        used += cost
    selected.reverse()
    return selected


# ============================================================== 拼装文本


_KIND_HEADER = {
    MemoryKind.MONTHLY: "[月度]",
    MemoryKind.WEEKLY: "[周]",
    MemoryKind.DAILY: "[日]",
    MemoryKind.SESSION: "[会话片段]",
}


def _format_memory_block(
    selected: dict[MemoryKind, list[MemoryEntry]],
) -> str:
    """把多层记忆拼成 <long_term_memory> 段落,空层级直接省略。"""
    sections: list[str] = []
    for kind in (MemoryKind.MONTHLY, MemoryKind.WEEKLY, MemoryKind.DAILY, MemoryKind.SESSION):
        entries = selected.get(kind) or []
        if not entries:
            continue
        for entry in entries:
            sections.append(_format_entry(entry))
    if not sections:
        return ""
    body = "\n\n".join(sections)
    return f"<long_term_memory>\n{body}\n</long_term_memory>"


def _format_entry(entry: MemoryEntry) -> str:
    """把单条记忆条目格式化成 prompt 中显示的小段。"""
    header = _KIND_HEADER.get(entry.kind, "[记忆]")
    period = _format_period_label(entry)
    title = f"{header} {period}".strip()
    summary = entry.summary.strip()
    return f"{title}\n{summary}"


def _format_period_label(entry: MemoryEntry) -> str:
    """根据层级生成更易读的时间标签。"""
    start = entry.period_start
    end = entry.period_end
    if entry.kind is MemoryKind.MONTHLY:
        return f"{start.year:04d}-{start.month:02d}"
    if entry.kind is MemoryKind.WEEKLY:
        iso = start.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    if entry.kind is MemoryKind.DAILY:
        return start.date().isoformat()
    # SESSION: 用日期 + 起止时分。
    same_day = start.date() == end.date()
    if same_day:
        return f"{start.date().isoformat()} {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return f"{start.isoformat(timespec='minutes')}~{end.isoformat(timespec='minutes')}"
