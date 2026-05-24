from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from ..memory.tokens import count_message_tokens, count_text_tokens

_SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。
"""

_MEMORY_HEADER = "以下是可用的长期记忆，按时间顺序提供；若与最近对话冲突，以最近对话为准。"

_MEMORY_KIND_ORDER = {
    "monthly": 0,
    "weekly": 1,
    "daily": 2,
    "session": 3,
}


@dataclass(frozen=True)
class PromptContent:
    """发送给 LLM 前的提示内容，系统提示词和历史消息分开保存。"""

    system: str
    messages: list[dict[str, str]]


@dataclass(frozen=True)
class _Period:
    start: datetime
    end: datetime
    label: str


def build_messages(
    events: list[dict[str, object]],
    memories: list[dict[str, object]] | None = None,
    *,
    recent_turns: int = 5,
    token_budget: int = 24000,
    today: date | None = None,
) -> PromptContent:
    """根据记忆和今日事件返回系统提示词与对话 messages。"""

    # 确定当前日期
    current_date = today or datetime.now().astimezone().date()

    # 选择记忆和事件，优先保证近期对话完整，再尽可能多地带入长期记忆，最后裁剪到 token 预算内。
    selected_memories = _select_memories(memories or [], today=current_date)

    # 最近对话事件只保留当天的，并且优先保证最近几轮完整。
    raw_events = _recent_today_events(events, today=current_date, recent_turns=recent_turns)

    # 组装成提示内容，并裁剪到 token 预算内。
    return _fit_prompt_budget(
        selected_memories,
        raw_events,
        token_budget=token_budget,
    )


def _fit_prompt_budget(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    token_budget: int,
) -> PromptContent:
    kept_memories = list(memories)
    kept_events = list(raw_events)

    while kept_memories:
        prompt = _build_prompt(kept_memories, kept_events)
        if _prompt_tokens(prompt) <= token_budget:
            return prompt
        kept_memories.pop(0)

    while len(kept_events) > 1:
        prompt = _build_prompt([], kept_events)
        if _prompt_tokens(prompt) <= token_budget:
            return prompt
        kept_events.pop(0)

    return _build_prompt([], kept_events)


def _build_prompt(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
) -> PromptContent:
    system = _SYSTEM_PROMPT
    memory_block = _memory_block(memories)
    if memory_block:
        system = f"{system}\n{memory_block}"
    return PromptContent(system=system, messages=_history_messages(raw_events))


def _prompt_tokens(prompt: PromptContent) -> int:
    return count_text_tokens(prompt.system) + count_message_tokens(prompt.messages)


def _select_memories(
    memories: list[dict[str, object]],
    *,
    today: date,
) -> list[dict[str, object]]:
    selected: list[tuple[_Period, dict[str, object]]] = []
    occupied: list[_Period] = []
    today_start = datetime.combine(today, time.min).astimezone()

    for memory in sorted(memories, key=_memory_sort_key):
        kind = str(memory.get("kind", ""))
        if kind not in _MEMORY_KIND_ORDER:
            continue
        period = _memory_period(memory)
        if period is None:
            continue
        if kind == "session" and period.start.astimezone().date() != today:
            continue
        if kind != "session" and period.end > today_start:
            continue
        if _overlaps_any(period, occupied):
            continue
        occupied.append(period)
        selected.append((period, memory))

    return [memory for period, memory in sorted(selected, key=lambda item: item[0].start)]


def _memory_sort_key(memory: dict[str, object]) -> tuple[int, datetime]:
    kind = str(memory.get("kind", ""))
    period = _memory_period(memory)
    start = period.start if period else datetime.max.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (_MEMORY_KIND_ORDER.get(kind, 99), start)


def _memory_period(memory: dict[str, object]) -> _Period | None:
    raw_period = memory.get("period")
    if not isinstance(raw_period, dict):
        return None
    try:
        start = _parse_datetime(str(raw_period["start"]))
        end = _parse_datetime(str(raw_period["end"]))
    except (KeyError, ValueError):
        return None
    if end <= start:
        return None
    return _Period(
        start=start,
        end=end,
        label=str(raw_period.get("label", "")),
    )


def _overlaps_any(period: _Period, existing: list[_Period]) -> bool:
    return any(period.start < item.end and item.start < period.end for item in existing)


def _memory_block(memories: list[dict[str, object]]) -> str:
    if not memories:
        return ""
    lines = [_MEMORY_HEADER]
    for memory in memories:
        period = memory.get("period")
        label = ""
        if isinstance(period, dict):
            label = str(period.get("label", ""))
        kind = str(memory.get("kind", "memory"))
        content = str(memory.get("content", "")).strip()
        if content:
            lines.append(f"- [{kind} {label}] {content}")
    return "\n".join(lines)


def _recent_today_events(
    events: list[dict[str, object]],
    *,
    today: date,
    recent_turns: int,
) -> list[dict[str, object]]:
    today_events = [
        event
        for event in sorted(events, key=lambda item: str(item.get("timestamp", "")))
        if _event_date(event) == today and _is_dialogue_event(event)
    ]
    if recent_turns <= 0:
        return today_events[-1:] if today_events else []

    user_indexes = [
        index for index, event in enumerate(today_events) if event.get("type") == "user_message"
    ]
    if len(user_indexes) <= recent_turns:
        return today_events
    return today_events[user_indexes[-recent_turns] :]


def _history_messages(events: list[dict[str, object]]) -> list[dict[str, str]]:
    """把历史事件转换成 LLM 对话消息。"""
    messages: list[dict[str, str]] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "user_message":
            messages.append({"role": "user", "content": str(event.get("content", ""))})
        elif event_type in {
            "assistant_response",
            "assistant_natural_response",
            "assistant_question",
        }:
            messages.append({"role": "assistant", "content": str(event.get("content", ""))})
    return messages


def _is_dialogue_event(event: dict[str, object]) -> bool:
    """判断事件是否属于对话事件，当前支持用户消息和助手回复两大类。"""
    return event.get("type") in {
        "user_message",
        "assistant_response",
        "assistant_natural_response",
        "assistant_question",
    }


def _event_date(event: dict[str, object]) -> date | None:
    try:
        return _parse_datetime(str(event.get("timestamp", ""))).date()
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()
