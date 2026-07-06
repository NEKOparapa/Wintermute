from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from ...memory.tokens import count_message_tokens, count_text_tokens
from ..storage.profile_store import ProfileStore
from ..storage.schedule_store import ScheduleStore
from .messages import build_history_messages, is_dialogue_event
from .types import PromptContent

logger = logging.getLogger(__name__)

_MEMORY_HEADER = "以下是可用的长期记忆，按时间顺序提供；若与最近对话冲突，以最近对话为准。"

_L2_EVENT_MEMORY_HEADER = "以下是今天发生但未进入对话的 L2 背景事件，按时间顺序提供："

_L3_EVENT_MEMORY_HEADER = "以下是今天发生但未进入对话的 L3 背景事件，按时间顺序提供："

_L1_CONTEXT_HEADER = "以下是今天 L1 主动唤醒处理过的事件摘要；用户提到“刚才那个”“那个日程”等指代时优先参考："

_SCHEDULE_CONTEXT_HEADER = "以下是当前日程表上下文（已取消日程已排除，最多 30 条）："

_ACTIVE_L1_EVENT_HEADER = "当前 L1 主动触发事件："

_MEMORY_KIND_ORDER = {
    "monthly": 0,
    "weekly": 1,
    "daily": 2,
    "session": 3,
}


@dataclass(frozen=True)
class _Period:
    start: datetime
    end: datetime
    label: str


def identity_block(profile_store: ProfileStore) -> str:
    """读取 soul，构成 AI 的固定身份描述。"""
    return profile_store.read_soul().strip()


def build_prompt_with_budget(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    identity: str,
    user_profile: str,
    system_prompt: str,
    l2_event_memories: list[dict[str, object]],
    l3_event_memories: list[dict[str, object]],
    l1_context_memories: list[dict[str, object]],
    schedule_items: list[dict[str, object]],
    active_l1_event: dict[str, object] | None = None,
    token_budget: int,
) -> PromptContent:
    kept_memories = list(memories)
    kept_events = list(raw_events)

    while kept_memories:
        prompt = _build_prompt(
            kept_memories,
            kept_events,
            identity=identity,
            user_profile=user_profile,
            system_prompt=system_prompt,
            l2_event_memories=l2_event_memories,
            l3_event_memories=l3_event_memories,
            l1_context_memories=l1_context_memories,
            schedule_items=schedule_items,
            active_l1_event=active_l1_event,
        )
        if _prompt_tokens(prompt) <= token_budget:
            return prompt
        kept_memories.pop(0)

    while len(kept_events) > 1:
        prompt = _build_prompt(
            [],
            kept_events,
            identity=identity,
            user_profile=user_profile,
            system_prompt=system_prompt,
            l2_event_memories=l2_event_memories,
            l3_event_memories=l3_event_memories,
            l1_context_memories=l1_context_memories,
            schedule_items=schedule_items,
            active_l1_event=active_l1_event,
        )
        if _prompt_tokens(prompt) <= token_budget:
            return prompt
        kept_events.pop(0)

    return _build_prompt(
        [],
        kept_events,
        identity=identity,
        user_profile=user_profile,
        system_prompt=system_prompt,
        l2_event_memories=l2_event_memories,
        l3_event_memories=l3_event_memories,
        l1_context_memories=l1_context_memories,
        schedule_items=schedule_items,
        active_l1_event=active_l1_event,
    )


def select_memories(
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


def sorted_event_memories(memories: list[dict[str, object]]) -> list[dict[str, object]]:
    """事件记忆按 period.start 升序排列，保证「今日事件」按时间顺序呈现。"""
    return sorted(memories, key=_event_memory_sort_key)


def schedule_prompt_items(data_dir: object, event_date: date) -> list[dict[str, object]]:
    try:
        return ScheduleStore(data_dir).schedules_for_prompt(day=event_date, days=7, limit=30)
    except Exception:
        logger.exception("日程上下文读取失败")
        return []


def recent_today_events(
    events: list[dict[str, object]],
    *,
    today: date,
    recent_turns: int,
) -> list[dict[str, object]]:
    today_events = [
        event
        for event in sorted(events, key=lambda item: str(item.get("timestamp", "")))
        if _event_date(event) == today and is_dialogue_event(event)
    ]
    if recent_turns <= 0:
        return today_events[-1:] if today_events else []

    user_indexes = [
        index for index, event in enumerate(today_events) if event.get("type") == "user_message"
    ]
    if len(user_indexes) <= recent_turns:
        return today_events
    return today_events[user_indexes[-recent_turns] :]


def _build_prompt(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    system_prompt: str,
    identity: str = "",
    user_profile: str = "",
    l2_event_memories: list[dict[str, object]] | None = None,
    l3_event_memories: list[dict[str, object]] | None = None,
    l1_context_memories: list[dict[str, object]] | None = None,
    schedule_items: list[dict[str, object]] | None = None,
    active_l1_event: dict[str, object] | None = None,
) -> PromptContent:
    parts: list[str] = []
    if identity.strip():
        parts.append(identity.strip())
    parts.append(system_prompt)
    if user_profile.strip():
        parts.append(f"## 关于用户\n{user_profile.strip()}")
    memory_block = _memory_block(memories)
    if memory_block:
        parts.append(memory_block)
    l2_event_memory_block = _event_memory_block(
        _L2_EVENT_MEMORY_HEADER,
        l2_event_memories or [],
    )
    if l2_event_memory_block:
        parts.append(l2_event_memory_block)
    l3_event_memory_block = _event_memory_block(
        _L3_EVENT_MEMORY_HEADER,
        l3_event_memories or [],
    )
    if l3_event_memory_block:
        parts.append(l3_event_memory_block)
    l1_context_block = _l1_context_block(l1_context_memories or [])
    if l1_context_block:
        parts.append(l1_context_block)
    schedule_block = _schedule_block(schedule_items or [])
    if schedule_block:
        parts.append(schedule_block)
    active_l1_event_block = _active_l1_event_block(active_l1_event)
    if active_l1_event_block:
        parts.append(active_l1_event_block)
    return PromptContent(system="\n\n".join(parts), messages=build_history_messages(raw_events))


def _prompt_tokens(prompt: PromptContent) -> int:
    return count_text_tokens(prompt.system) + count_message_tokens(prompt.messages)


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


def _event_memory_sort_key(memory: dict[str, object]) -> str:
    period = memory.get("period")
    if isinstance(period, dict):
        return str(period.get("start", ""))
    return ""


def _event_memory_block(header: str, memories: list[dict[str, object]]) -> str:
    if not memories:
        return ""
    lines = [header]
    for memory in memories:
        content = str(memory.get("content", "")).strip()
        if not content:
            continue
        metadata = memory.get("metadata")
        source = ""
        if isinstance(metadata, dict):
            source = str(metadata.get("event_source", "")).strip()
        prefix = f"[{source}] " if source else ""
        lines.append(f"- {prefix}{content}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _l1_context_block(memories: list[dict[str, object]]) -> str:
    if not memories:
        return ""
    lines = [_L1_CONTEXT_HEADER]
    for memory in memories:
        content = str(memory.get("content", "")).strip()
        if not content:
            continue
        metadata = memory.get("metadata")
        source = ""
        if isinstance(metadata, dict):
            source = str(metadata.get("event_source", "")).strip()
        prefix = f"[{source}] " if source else ""
        lines.append(f"- {prefix}{content}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _schedule_block(items: list[dict[str, object]]) -> str:
    if not items:
        return ""
    lines = [_SCHEDULE_CONTEXT_HEADER]
    for item in items:
        if not isinstance(item, dict):
            continue
        schedule = item.get("schedule")
        if not isinstance(schedule, dict):
            continue
        title = str(schedule.get("title", "")).strip()
        if not title:
            continue
        category = str(item.get("category", "")).strip()
        label = _schedule_category_label(category)
        when = str(item.get("time") or schedule.get("next_trigger_at") or "").strip()
        schedule_id = str(schedule.get("id", "")).strip()
        content = str(schedule.get("content", "")).strip()
        recurrence = _schedule_recurrence_label(schedule.get("recurrence"))
        id_part = f" id={schedule_id}" if schedule_id else ""
        recurrence_part = f" {recurrence}" if recurrence else ""
        content_part = f": {content}" if content else ""
        lines.append(f"- [{label}] {when}{id_part} {title}{recurrence_part}{content_part}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _schedule_category_label(category: str) -> str:
    if category == "overdue":
        return "逾期未触发"
    if category == "upcoming":
        return "未来7天"
    if category == "triggered_today":
        return "今日已触发"
    return "日程"


def _schedule_recurrence_label(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    frequency = str(value.get("frequency") or "none").strip().lower()
    if frequency == "none":
        return ""
    interval = str(value.get("interval") or "1").strip()
    unit = {"daily": "天", "weekly": "周", "monthly": "月"}.get(frequency)
    if not unit:
        return ""
    until = str(value.get("until") or "").strip()
    until_part = f"，截止 {until}" if until else ""
    return f"（每 {interval} {unit}重复{until_part}）"


def _active_l1_event_block(event: dict[str, object] | None) -> str:
    if not event:
        return ""
    source = str(event.get("source", "")).strip() or "unknown"
    event_type = str(event.get("type", "")).strip() or "l1_trigger"
    timestamp = str(event.get("timestamp", "")).strip()
    content = str(event.get("content", "")).strip()
    if not content:
        return ""
    when = f"{timestamp} " if timestamp else ""
    return f"{_ACTIVE_L1_EVENT_HEADER}\n- {when}{source} {event_type}: {content}"


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
