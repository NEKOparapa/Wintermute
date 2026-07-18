from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from ...config.config import get_settings
from ...memory.tokens import count_message_tokens, count_text_tokens
from ..storage.profile_store import ProfileStore
from ..storage.schedule_store import ScheduleStore
from ..storage.storage import GlobalEventStore, MemoryStore
from ..storage.subagent_task_store import SubagentTaskStore
from .messages import build_history_messages, is_dialogue_event
from .subagent_task_context import (
    iter_compact_subagent_task_snapshots,
    load_subagent_task_snapshot,
    subagent_task_context_block,
)
from .types import PromptContent

logger = logging.getLogger(__name__)

_L0_SYSTEM_PROMPT = """我是 Wintermute，一个长期陪伴用户的个人 AI。

你可以使用 subagent_task 工具，把适合后台异步执行且目标自包含的工作委派给子代理，
并根据任务上下文了解其当前进度。
"""

_MEMORY_HEADER = "以下是可用的长期记忆，按时间顺序提供；若与最近对话冲突，以最近对话为准。"

_L2_EVENT_MEMORY_HEADER = "以下是今天发生但未进入对话的 L2 背景事件，按时间顺序提供："

_L3_EVENT_MEMORY_HEADER = "以下是今天发生但未进入对话的 L3 背景事件，按时间顺序提供："

_L1_CONTEXT_HEADER = "以下是今天 L1 主动唤醒处理过的事件摘要；用户提到“刚才那个”“那个日程”等指代时优先参考："

_SCHEDULE_CONTEXT_HEADER = "以下是当前日程表上下文（已取消日程已排除，最多 30 条）："

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


def build_messages(event_date: date) -> PromptContent:
    """兼容旧调用；等价于构建 L0 prompt。"""
    return build_l0_messages(event_date)


def build_l0_messages(
    event_date: date,
    *,
    task_store: SubagentTaskStore | None = None,
) -> PromptContent:
    """构建 L0 用户主动对话 prompt。"""
    settings = get_settings()
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    schedule_items = schedule_prompt_items(settings.data_dir, event_date)
    resolved_task_store = (
        task_store if task_store is not None else SubagentTaskStore(settings.data_dir)
    )
    task_snapshot = load_subagent_task_snapshot(resolved_task_store)

    # 长期画像（soul/user）作为固定身份上下文，始终注入且不参与 token 裁剪。
    identity = ""
    user_profile = ""
    if settings.profile_enabled:
        profile_store = ProfileStore(
            settings.data_dir,
            soul_path=settings.soul_path,
            user_template_path=settings.user_template_path,
        )
        identity = identity_block(profile_store)
        user_profile = profile_store.read_user().strip()

    # 选择记忆和事件，优先保证近期对话完整，再尽可能多地带入长期记忆，最后裁剪到 token 预算内。
    selected_memories = select_memories(memory_store.load_all_memories(), today=event_date)

    # 当天的 L1 上下文记忆注入
    l1_context_memories = sorted_event_memories(
        memory_store.load_l1_context_memories(event_date.isoformat())
    )
    # 当天的 L2 上下文记忆注入
    l2_event_memories = sorted_event_memories(
        memory_store.load_l2_event_memories(event_date.isoformat())
    )
    # 当天的 L3 上下文记忆注入
    l3_event_memories = sorted_event_memories(
        memory_store.load_l3_event_memories(event_date.isoformat())
    )

    # 最近 L0 对话事件只保留当天的，并且优先保证最近几轮完整。
    raw_events = recent_today_events(
        event_store.load_events_for_date(event_date),
        today=event_date,
        recent_turns=settings.prompt_recent_turns,
    )

    # 组装提示词上下文
    return _build_l0_prompt_with_budget(
        selected_memories,
        raw_events,
        identity=identity,
        user_profile=user_profile,
        l2_event_memories=l2_event_memories,
        l3_event_memories=l3_event_memories,
        l1_context_memories=l1_context_memories,
        task_snapshot=task_snapshot,
        schedule_items=schedule_items,
        token_budget=settings.prompt_token_budget,
    )


# 构建 L0 提示词并控制 token 数量
def _build_l0_prompt_with_budget(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    identity: str,
    user_profile: str,
    l1_context_memories: list[dict[str, object]],
    l2_event_memories: list[dict[str, object]],
    l3_event_memories: list[dict[str, object]],
    task_snapshot: object = None,
    schedule_items: list[dict[str, object]],
    token_budget: int,
) -> PromptContent:
    
    # 先尝试保留所有记忆和事件
    kept_memories = list(memories)
    kept_events = list(raw_events)

    # 如果超出 token 预算，先裁剪记忆，再裁剪事件，直到满足预算
    while kept_memories:
        prompt = _build_l0_prompt(
            kept_memories,
            kept_events,
            identity=identity,
            user_profile=user_profile,
            l1_context_memories=l1_context_memories,
            l2_event_memories=l2_event_memories,
            l3_event_memories=l3_event_memories,
            task_snapshot=task_snapshot,
            schedule_items=schedule_items,
        )
        if prompt_tokens(prompt) <= token_budget:
            return prompt
        kept_memories.pop(0)

    # 如果裁剪完所有记忆仍超出 token 预算，则裁剪事件，保证至少保留最近一条事件
    while len(kept_events) > 1:
        prompt = _build_l0_prompt(
            [],
            kept_events,
            identity=identity,
            user_profile=user_profile,
            l1_context_memories=l1_context_memories,
            l2_event_memories=l2_event_memories,
            l3_event_memories=l3_event_memories,
            task_snapshot=task_snapshot,
            schedule_items=schedule_items,
        )
        if prompt_tokens(prompt) <= token_budget:
            return prompt
        kept_events.pop(0)

    # 仍超预算时，丢弃终态摘要和长文本，但保留全部活跃任务 ID、状态与进度。
    prompt = _build_l0_prompt(
        [],
        kept_events,
        identity=identity,
        user_profile=user_profile,
        l1_context_memories=l1_context_memories,
        l2_event_memories=l2_event_memories,
        l3_event_memories=l3_event_memories,
        task_snapshot=task_snapshot,
        schedule_items=schedule_items,
    )
    if prompt_tokens(prompt) <= token_budget:
        return prompt
    compact_prompt = prompt
    for compact_snapshot in iter_compact_subagent_task_snapshots(task_snapshot):
        compact_prompt = _build_l0_prompt(
            [],
            kept_events,
            identity=identity,
            user_profile=user_profile,
            l1_context_memories=l1_context_memories,
            l2_event_memories=l2_event_memories,
            l3_event_memories=l3_event_memories,
            task_snapshot=compact_snapshot,
            schedule_items=schedule_items,
        )
        if prompt_tokens(compact_prompt) <= token_budget:
            return compact_prompt
    return compact_prompt

# 构建 L0 提示词
def _build_l0_prompt(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    identity: str,
    user_profile: str,
    l1_context_memories: list[dict[str, object]],
    l2_event_memories: list[dict[str, object]],
    l3_event_memories: list[dict[str, object]],
    task_snapshot: object = None,
    schedule_items: list[dict[str, object]],
) -> PromptContent:
    system_parts = [
        _L0_SYSTEM_PROMPT.strip(),  # L0 默认前缀提示词块
        identity.strip(),  # soul 人格设定模块
        _user_profile_block(user_profile),  # 用户信息模块
        l1_context_block(l1_context_memories),  # L1 历史事件信息
        l2_event_memory_block(l2_event_memories),  # L2 事件消息
        l3_event_memory_block(l3_event_memories),  # L3 事件消息
        memory_block(memories),  # 长期记忆
        subagent_task_context_block(task_snapshot),  # 子代理任务上下文
        schedule_block(schedule_items),  # 日程上下文
    ]
    return PromptContent(
        system=_join_prompt_blocks(system_parts),
        messages=build_history_messages(raw_events),
    )

# 构建用户信息模块
def _user_profile_block(user_profile: str) -> str:
    user_profile = user_profile.strip()
    if not user_profile:
        return ""
    return f"## 关于用户\n{user_profile}"

# 连接提示词块
def _join_prompt_blocks(blocks: list[str]) -> str:
    return "\n\n".join(block.strip() for block in blocks if block.strip())

# 构建身份模块
def identity_block(profile_store: ProfileStore) -> str:
    """读取 soul，构成 AI 的固定身份描述。"""
    return profile_store.read_soul().strip()

# 选择记忆，保证近期对话完整，再尽可能多地带入长期记忆
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

# 对事件记忆进行排序
def sorted_event_memories(memories: list[dict[str, object]]) -> list[dict[str, object]]:
    """事件记忆按 period.start 升序排列，保证「今日事件」按时间顺序呈现。"""
    return sorted(memories, key=_event_memory_sort_key)

# 获取日程提示项
def schedule_prompt_items(data_dir: object, event_date: date) -> list[dict[str, object]]:
    try:
        return ScheduleStore(data_dir).schedules_for_prompt(day=event_date, days=7, limit=30)
    except Exception:
        logger.exception("日程上下文读取失败")
        return []

# 获取当天的最近对话事件
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

# 计算提示词的 token 数量
def prompt_tokens(prompt: PromptContent) -> int:
    return count_text_tokens(prompt.system) + count_message_tokens(prompt.messages)

# 构建记忆块
def memory_block(memories: list[dict[str, object]]) -> str:
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

# 构建 L1 上下文块
def l1_context_block(memories: list[dict[str, object]]) -> str:
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

# 构建 L2 事件记忆块
def l2_event_memory_block(memories: list[dict[str, object]]) -> str:
    return _event_memory_block(_L2_EVENT_MEMORY_HEADER, memories)

# 构建 L3 事件记忆块
def l3_event_memory_block(memories: list[dict[str, object]]) -> str:
    return _event_memory_block(_L3_EVENT_MEMORY_HEADER, memories)

# 构建日程上下文块
def schedule_block(items: list[dict[str, object]]) -> str:
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

# 获取记忆的排序键
def _memory_sort_key(memory: dict[str, object]) -> tuple[int, datetime]:
    kind = str(memory.get("kind", ""))
    period = _memory_period(memory)
    start = period.start if period else datetime.max.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (_MEMORY_KIND_ORDER.get(kind, 99), start)

# 解析记忆的时间周期
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

# 检查时间周期是否与已有周期重叠
def _overlaps_any(period: _Period, existing: list[_Period]) -> bool:
    return any(period.start < item.end and item.start < period.end for item in existing)

# 获取事件记忆的排序键
def _event_memory_sort_key(memory: dict[str, object]) -> str:
    period = memory.get("period")
    if isinstance(period, dict):
        return str(period.get("start", ""))
    return ""

# 构建事件记忆块
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

# 获取日程类别标签
def _schedule_category_label(category: str) -> str:
    if category == "overdue":
        return "逾期未触发"
    if category == "upcoming":
        return "未来7天"
    if category == "triggered_today":
        return "今日已触发"
    return "日程"

# 获取日程重复标签
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

# 获取事件的日期
def _event_date(event: dict[str, object]) -> date | None:
    try:
        return _parse_datetime(str(event.get("timestamp", ""))).date()
    except ValueError:
        return None

# 解析 ISO 格式的日期时间字符串为 datetime 对象
def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()
