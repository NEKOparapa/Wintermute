from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from ..config.config import get_settings
from ..storage.storage import GlobalEventStore, MemoryStore
from .tokens import count_event_tokens

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = """你负责把历史事件压缩成可供未来对话使用的中文记忆。

要求：
- 只输出摘要正文。
- 保留事实、偏好、待办、决定和长期有用的上下文。
- 省略寒暄、重复内容和无长期价值的过程描述。
- 不编造原始事件中没有的信息。
"""


@dataclass(frozen=True)
class ConsolidationResult:
    """一次压缩任务的结果。"""

    created: bool
    memory: dict[str, Any] | None = None
    reason: str = ""


class MemoryConsolidator:
    """把 raw events 或低层记忆压缩成 session/daily/weekly/monthly 记忆。"""

    def __init__(
        self,
        event_store: GlobalEventStore,
        memory_store: MemoryStore,
        llm,
    ) -> None:
        self.event_store = event_store
        self.memory_store = memory_store
        self.llm = llm

    def auto_consolidate_session_for_event(self, user_event: dict[str, object]) -> date:
        """根据用户事件日期自动判断并压缩 session，失败时只记录日志。"""
        current_date = _event_date(user_event)
        try:
            self.maybe_consolidate_session(today=current_date)
        except Exception:
            logger.exception("session 记忆压缩失败，继续使用当前 raw events")
        return current_date

    def maybe_consolidate_session(self, *, today: date | None = None) -> ConsolidationResult:
        """当今天 raw events 超过阈值时，压缩最近 N 轮之前的未压缩事件。"""
        current_date = today or datetime.now().astimezone().date()
        events = self.event_store.load_events_for_date(current_date)
        token_count = count_event_tokens(events)
        if token_count <= get_settings().session_token_threshold:
            return ConsolidationResult(False, reason="below_threshold")

        candidates = self._session_candidates(events)
        compressed_ids = self.memory_store.source_event_ids_for_session(current_date.isoformat())
        candidates = [
            event
            for event in candidates
            if str(event.get("id", "")) and str(event.get("id", "")) not in compressed_ids
        ]
        if not candidates:
            return ConsolidationResult(False, reason="no_uncompressed_events")

        label = current_date.isoformat()
        content = self._summarize_events("session", label, candidates)
        memory = self.memory_store.save_memory(
            kind="session",
            label=label,
            period=_event_period(candidates, fallback_date=current_date, label=label),
            content=content,
            source_event_ids=[str(event["id"]) for event in candidates if event.get("id")],
            metadata={"token_count": token_count},
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def consolidate_daily(self, target_date: date) -> ConsolidationResult:
        """压缩指定自然日的全部 raw events；目标文件存在时跳过。"""
        label = target_date.isoformat()
        if self.memory_store.memory_exists("daily", label):
            return ConsolidationResult(False, reason="exists")

        events = self.event_store.load_events_for_date(target_date)
        if events:
            content = self._summarize_events("daily", label, events)
        else:
            content = "当天没有记录事件。"
        memory = self.memory_store.save_memory(
            kind="daily",
            label=label,
            period=_day_period(target_date),
            content=content,
            source_event_ids=[str(event["id"]) for event in events if event.get("id")],
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def consolidate_weekly(self, week_start: date) -> ConsolidationResult:
        """压缩指定 ISO 周；缺任一天 daily 时跳过。"""
        week_start = week_start - timedelta(days=week_start.weekday())
        label = _week_label(week_start)
        if self.memory_store.memory_exists("weekly", label):
            return ConsolidationResult(False, reason="exists")

        daily_memories: list[dict[str, Any]] = []
        for offset in range(7):
            daily_label = (week_start + timedelta(days=offset)).isoformat()
            memory = self.memory_store.load_memory("daily", daily_label)
            if memory is None:
                return ConsolidationResult(False, reason=f"missing_daily:{daily_label}")
            daily_memories.append(memory)

        content = self._summarize_memories("weekly", label, daily_memories)
        memory = self.memory_store.save_memory(
            kind="weekly",
            label=label,
            period=_week_period(week_start),
            content=content,
            source_memory_ids=[str(item["id"]) for item in daily_memories if item.get("id")],
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def consolidate_monthly(self, month_start: date) -> ConsolidationResult:
        """压缩指定自然月内所有已存在且 period 相交的 weekly 记忆。"""
        month_start = month_start.replace(day=1)
        label = month_start.strftime("%Y-%m")
        if self.memory_store.memory_exists("monthly", label):
            return ConsolidationResult(False, reason="exists")

        period = _month_period(month_start)
        weekly_memories = [
            memory
            for memory in self.memory_store.load_all_memories()
            if memory.get("kind") == "weekly" and _memory_overlaps(memory, period)
        ]
        weekly_memories.sort(key=lambda item: str(item.get("period", {}).get("start", "")))
        if weekly_memories:
            content = self._summarize_memories("monthly", label, weekly_memories)
        else:
            content = "上月没有可用周记忆。"
        memory = self.memory_store.save_memory(
            kind="monthly",
            label=label,
            period=period,
            content=content,
            source_memory_ids=[str(item["id"]) for item in weekly_memories if item.get("id")],
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def _session_candidates(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        dialogue_events = [event for event in events if _is_dialogue_event(event)]
        user_indexes = [
            index for index, event in enumerate(dialogue_events) if event.get("type") == "user_message"
        ]
        recent_turns = get_settings().prompt_recent_turns
        if len(user_indexes) <= recent_turns:
            return []
        return dialogue_events[: user_indexes[-recent_turns]]

    def _summarize_events(
        self,
        kind: str,
        label: str,
        events: list[dict[str, Any]],
    ) -> str:
        return self.llm.complete(
            system=_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"请压缩 {kind} {label} 的原始事件：\n\n{_format_events(events)}",
                }
            ],
        )

    def _summarize_memories(
        self,
        kind: str,
        label: str,
        memories: list[dict[str, Any]],
    ) -> str:
        return self.llm.complete(
            system=_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"请压缩 {kind} {label} 的下层记忆：\n\n{_format_memories(memories)}",
                }
            ],
        )


def previous_day(now: datetime | None = None) -> date:
    current = now or datetime.now().astimezone()
    return current.date() - timedelta(days=1)


def previous_week_start(now: datetime | None = None) -> date:
    current = now or datetime.now().astimezone()
    this_week_start = current.date() - timedelta(days=current.date().weekday())
    return this_week_start - timedelta(days=7)


def previous_month_start(now: datetime | None = None) -> date:
    current = now or datetime.now().astimezone()
    first_this_month = current.date().replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    return last_prev_month.replace(day=1)


def _format_events(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events:
        lines.append(
            f"- {event.get('timestamp')} {event.get('source')} {event.get('type')}: "
            f"{event.get('content', '')}"
        )
    return "\n".join(lines)


def _format_memories(memories: list[dict[str, Any]]) -> str:
    lines = []
    for memory in memories:
        period = memory.get("period", {})
        label = period.get("label", "") if isinstance(period, dict) else ""
        lines.append(f"- {memory.get('kind')} {label}: {memory.get('content', '')}")
    return "\n".join(lines)


def _event_period(events: list[dict[str, Any]], *, fallback_date: date, label: str) -> dict[str, str]:
    if not events:
        return _day_period(fallback_date)
    start = _parse_datetime(str(events[0].get("timestamp", "")))
    end = _parse_datetime(str(events[-1].get("timestamp", ""))) + timedelta(seconds=1)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": label,
    }


def _day_period(target_date: date) -> dict[str, str]:
    start = datetime.combine(target_date, time.min).astimezone()
    end = start + timedelta(days=1)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": target_date.isoformat(),
    }


def _week_period(week_start: date) -> dict[str, str]:
    start = datetime.combine(week_start, time.min).astimezone()
    end = start + timedelta(days=7)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": _week_label(week_start),
    }


def _month_period(month_start: date) -> dict[str, str]:
    start = datetime.combine(month_start, time.min).astimezone()
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    end = datetime.combine(next_month, time.min).astimezone()
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": month_start.strftime("%Y-%m"),
    }


def _week_label(week_start: date) -> str:
    iso_year, iso_week, _ = week_start.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _memory_overlaps(memory: dict[str, Any], period: dict[str, str]) -> bool:
    memory_period = memory.get("period")
    if not isinstance(memory_period, dict):
        return False
    try:
        left_start = _parse_datetime(str(memory_period["start"]))
        left_end = _parse_datetime(str(memory_period["end"]))
        right_start = _parse_datetime(period["start"])
        right_end = _parse_datetime(period["end"])
    except (KeyError, ValueError):
        return False
    return left_start < right_end and right_start < left_end


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def _event_date(event: dict[str, object]) -> date:
    return _parse_datetime(str(event["timestamp"])).date()


def _is_dialogue_event(event: dict[str, Any]) -> bool:
    return event.get("type") in {
        "user_message",
        "assistant_response",
        "assistant_natural_response",
        "assistant_question",
    }
