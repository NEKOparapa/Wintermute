from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from ..config.config import get_settings
from ..memory.tokens import count_message_tokens, count_text_tokens
from ..profile.store import ProfileStore
from ..storage.schedule_store import ScheduleStore
from ..storage.storage import GlobalEventStore, MemoryStore

logger = logging.getLogger(__name__)

_L0_SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。
"""

_L1_SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手，当前正在处理 L1 主动唤醒事件。

L1 处理原则：
- 使用用户的语言。
- 只判断当前主动事件对用户是否有用，并给出可直接通知用户的简洁结果。
- 可以参考最近 L0 对话了解用户正在做什么，但不要延续 L0 对话语气。
- 不把当前事件当作用户提问。
- 不描述你的处理步骤。
"""

_MEMORY_HEADER = "以下是可用的长期记忆，按时间顺序提供；若与最近对话冲突，以最近对话为准。"

_EVENT_MEMORY_HEADER = "以下是今天发生但未进入对话的事件观测（L2/L3 背景事件），按时间顺序提供："

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
class PromptContent:
    """发送给 LLM 前的提示内容，系统提示词和历史消息分开保存。"""

    system: str
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class _Period:
    start: datetime
    end: datetime
    label: str


def build_l0_messages(
    event_date: date,
) -> PromptContent:
    """构建 L0 用户主动对话 prompt。"""
    settings = get_settings()
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    schedule_items = _schedule_prompt_items(settings.data_dir, event_date)

    # 长期画像（soul/persona/user）作为固定身份上下文，始终注入且不参与 token 裁剪。
    identity = ""
    user_profile = ""
    if settings.profile_enabled:
        profile_store = ProfileStore(
            settings.data_dir,
            soul_path=settings.soul_path,
            persona_template_path=settings.persona_template_path,
            user_template_path=settings.user_template_path,
        )
        identity = _identity_block(profile_store)
        user_profile = profile_store.read_user().strip()

    # 选择记忆和事件，优先保证近期对话完整，再尽可能多地带入长期记忆，最后裁剪到 token 预算内。
    selected_memories = _select_memories(memory_store.load_all_memories(), today=event_date)

    # 当天的事件记忆（L2/L3 背景事件逐条压缩后的结果）作为「今日事件」始终注入。
    event_memories = _sorted_event_memories(
        memory_store.load_event_memories(event_date.isoformat())
    )
    l1_context_memories = _sorted_event_memories(
        memory_store.load_l1_context_memories(event_date.isoformat())
    )

    # 最近 L0 对话事件只保留当天的，并且优先保证最近几轮完整。
    raw_events = _recent_today_events(
        event_store.load_events_for_date(event_date),
        today=event_date,
        recent_turns=settings.prompt_recent_turns,
    )

    # 组装成提示内容，并裁剪到 token 预算内。
    return _fit_prompt_budget(
        selected_memories,
        raw_events,
        identity=identity,
        user_profile=user_profile,
        system_prompt=_L0_SYSTEM_PROMPT,
        event_memories=event_memories,
        l1_context_memories=l1_context_memories,
        schedule_items=schedule_items,
        token_budget=settings.prompt_token_budget,
    )


def build_l1_messages(
    event_date: date,
    active_event: dict[str, object],
) -> PromptContent:
    """构建 L1 主动唤醒 prompt；读取 L0 最近对话，但不复用 L0 对话流程。"""
    settings = get_settings()
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    schedule_items = _schedule_prompt_items(settings.data_dir, event_date)

    identity = ""
    user_profile = ""
    if settings.profile_enabled:
        profile_store = ProfileStore(
            settings.data_dir,
            soul_path=settings.soul_path,
            persona_template_path=settings.persona_template_path,
            user_template_path=settings.user_template_path,
        )
        identity = _identity_block(profile_store)
        user_profile = profile_store.read_user().strip()

    selected_memories = _select_memories(memory_store.load_all_memories(), today=event_date)
    event_memories = _sorted_event_memories(
        memory_store.load_event_memories(event_date.isoformat())
    )
    l1_context_memories = _sorted_event_memories(
        memory_store.load_l1_context_memories(event_date.isoformat())
    )
    l0_recent_events = _recent_today_events(
        event_store.load_events_for_date(event_date),
        today=event_date,
        recent_turns=settings.prompt_recent_turns,
    )

    prompt = _fit_prompt_budget(
        selected_memories,
        l0_recent_events,
        identity=identity,
        user_profile=user_profile,
        system_prompt=_L1_SYSTEM_PROMPT,
        event_memories=event_memories,
        l1_context_memories=l1_context_memories,
        schedule_items=schedule_items,
        active_l1_event=active_event,
        token_budget=settings.prompt_token_budget,
    )
    return PromptContent(
        system=prompt.system,
        messages=[
            *prompt.messages,
            build_event_input_message(
                "请处理系统提示中的当前 L1 主动触发事件。",
                active_event,
            ),
        ],
    )


def build_messages(event_date: date) -> PromptContent:
    """兼容旧调用；等价于构建 L0 prompt。"""
    return build_l0_messages(event_date)


def build_event_input_message(text: str, event: dict[str, object]) -> dict[str, Any]:
    """Build one Responses user input item from an event and its attachments."""
    return _user_input_message(text, _event_attachments(event))


def build_events_input_message(
    text: str,
    events: list[dict[str, object]],
) -> dict[str, Any]:
    """Build one Responses user input item from multiple events and attachments."""
    attachments: list[object] = []
    for event in events:
        attachments.extend(_event_attachments(event))
    return _user_input_message(text, attachments)


def _identity_block(profile_store: ProfileStore) -> str:
    """拼接 soul + persona，构成 AI 的固定身份描述。"""
    parts = [profile_store.read_soul().strip(), profile_store.read_persona().strip()]
    return "\n\n".join(part for part in parts if part)


def _fit_prompt_budget(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    identity: str,
    user_profile: str,
    system_prompt: str,
    event_memories: list[dict[str, object]],
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
            event_memories=event_memories,
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
            event_memories=event_memories,
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
        event_memories=event_memories,
        l1_context_memories=l1_context_memories,
        schedule_items=schedule_items,
        active_l1_event=active_l1_event,
    )


def _build_prompt(
    memories: list[dict[str, object]],
    raw_events: list[dict[str, object]],
    *,
    system_prompt: str,
    identity: str = "",
    user_profile: str = "",
    event_memories: list[dict[str, object]] | None = None,
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
    event_memory_block = _event_memory_block(event_memories or [])
    if event_memory_block:
        parts.append(event_memory_block)
    l1_context_block = _l1_context_block(l1_context_memories or [])
    if l1_context_block:
        parts.append(l1_context_block)
    schedule_block = _schedule_block(schedule_items or [])
    if schedule_block:
        parts.append(schedule_block)
    active_l1_event_block = _active_l1_event_block(active_l1_event)
    if active_l1_event_block:
        parts.append(active_l1_event_block)
    return PromptContent(system="\n\n".join(parts), messages=_history_messages(raw_events))


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


def _sorted_event_memories(memories: list[dict[str, object]]) -> list[dict[str, object]]:
    """事件记忆按 period.start 升序排列，保证「今日事件」按时间顺序呈现。"""
    return sorted(memories, key=_event_memory_sort_key)


def _event_memory_sort_key(memory: dict[str, object]) -> str:
    period = memory.get("period")
    if isinstance(period, dict):
        return str(period.get("start", ""))
    return ""


def _event_memory_block(memories: list[dict[str, object]]) -> str:
    if not memories:
        return ""
    lines = [_EVENT_MEMORY_HEADER]
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


def _schedule_prompt_items(data_dir: object, event_date: date) -> list[dict[str, object]]:
    try:
        return ScheduleStore(data_dir).schedules_for_prompt(day=event_date, days=7, limit=30)
    except Exception:
        logger.exception("日程上下文读取失败")
        return []


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


def _history_messages(events: list[dict[str, object]]) -> list[dict[str, Any]]:
    """把历史事件转换成 Responses API 的 input 项列表。

    保留工具调用（function_call）与工具结果（function_call_output）以维持
    原生 tools 协议；用户消息可携带图片 / 音频 / 视频 / 文件等多模态附件。
    """
    messages: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        content = str(event.get("content", ""))
        if event_type == "user_message":
            messages.append(_user_input_message(content, metadata.get("attachments")))
        elif event_type in {
            "assistant_response",
            "assistant_natural_response",
            "assistant_question",
        }:
            messages.append({"role": "assistant", "content": content})
        elif event_type == "assistant_tool_call":
            messages.append(
                {
                    "type": "function_call",
                    "call_id": str(metadata.get("tool_call_id", "")),
                    "name": str(metadata.get("tool_name", "")),
                    "arguments": content,
                }
            )
        elif event_type == "tool_result":
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": str(metadata.get("tool_call_id", "")),
                    "output": content,
                }
            )
    return messages


def _user_input_message(text: str, attachments: object) -> dict[str, Any]:
    """构建一条用户 input 消息。无附件时用纯文本，否则用多模态 content 列表。"""
    if not isinstance(attachments, list) or not attachments:
        return {"role": "user", "content": text}

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "input_text", "text": text})
    for attachment in attachments:
        part = _attachment_content_part(attachment)
        if part is not None:
            parts.append(part)
    if not parts:
        parts.append({"type": "input_text", "text": text})
    return {"role": "user", "content": parts}


def _attachment_content_part(attachment: object) -> dict[str, Any] | None:
    """把一条附件字典映射成 Responses API 的 content part；无法表达时返回 None。"""
    if not isinstance(attachment, dict):
        return None

    kind = str(attachment.get("kind", "")).strip().lower()
    url = _opt(attachment.get("url"))
    data = _opt(attachment.get("data"))
    file_id = _opt(attachment.get("file_id"))
    mime = _opt(attachment.get("mime_type"))
    raw_part = attachment.get("content_part")
    if isinstance(raw_part, dict) and not file_id:
        return dict(raw_part)

    if kind == "image":
        part: dict[str, Any] = {
            "type": "input_image",
        }
        detail = _opt(attachment.get("detail"))
        if detail:
            part["detail"] = detail
        if file_id:
            part["file_id"] = file_id
        elif url:
            part["image_url"] = url
        elif data:
            part["image_url"] = _data_url(mime or "image/png", data)
        else:
            return None
        return part

    if kind == "audio":
        if file_id:
            return {"type": "input_audio", "file_id": file_id}
        if url:
            return {"type": "input_audio", "audio_url": url}
        if data:
            audio_mime = mime or _mime_from_data_url(data) or _audio_mime_from_format(
                _opt(attachment.get("format"))
            )
            return {
                "type": "input_audio",
                "audio_url": _data_url(audio_mime or "audio/mpeg", data),
            }
        return None

    if kind == "file":
        if file_id:
            return {"type": "input_file", "file_id": file_id}
        if url:
            return {"type": "input_file", "file_url": url}
        if data:
            return {
                "type": "input_file",
                "filename": _opt(attachment.get("filename")) or "file",
                "file_data": _data_url(mime or "application/octet-stream", data),
            }
        return None

    if kind == "video":
        # OpenAI 官方 Responses API 暂未原生支持视频，这里按兼容服务常见约定尽力表达。
        if file_id:
            return {"type": "input_video", "file_id": file_id}
        if url:
            return {"type": "input_video", "video_url": url}
        if data:
            return {"type": "input_video", "video_url": _data_url(mime or "video/mp4", data)}
        return None

    # content_part 直通：兼容服务需要特殊结构时，直接使用调用方给出的原始 part。
    if isinstance(raw_part, dict):
        return dict(raw_part)

    return None


def _event_attachments(event: dict[str, object]) -> list[object]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
    return list(attachments) if isinstance(attachments, list) else []


def _data_url(mime: str, data: str) -> str:
    """把 base64 原文包装成 data URL；已是 data URL 时原样返回。"""
    if data.startswith("data:"):
        return data
    return f"data:{mime};base64,{data}"


def _mime_from_data_url(data: str) -> str | None:
    """从 data URL 前缀里解析 MIME，例如 data:audio/mp3;base64,xxx。"""
    if not data.startswith("data:") or ":" not in data:
        return None
    header = data[len("data:") :].split(",", 1)[0]
    mime = header.split(";", 1)[0].strip()
    return mime or None


def _audio_mime_from_format(audio_format: str | None) -> str | None:
    """从简写音频格式推断 data URL 需要的 MIME。"""
    if not audio_format:
        return None
    lowered = audio_format.lower()
    if lowered == "mp3":
        return "audio/mpeg"
    if lowered == "wav":
        return "audio/wav"
    return None


def _opt(value: object) -> str | None:
    """把可选字段规范成非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_DIALOGUE_EVENT_TYPES = {
    "user_message",
    "assistant_response",
    "assistant_natural_response",
    "assistant_question",
    "assistant_tool_call",
    "tool_result",
}


def _is_dialogue_event(event: dict[str, object]) -> bool:
    """判断事件是否属于对话事件，覆盖用户消息、助手回复与工具调用/结果。"""
    if str(event.get("attention_level", "")).upper() == "L1":
        return False
    return event.get("type") in _DIALOGUE_EVENT_TYPES


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
