"""将设备观测和已完成的切片投影成可比较、有来源的会话消息。"""

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, JsonValue

from wintermute.agent import UserMessage
from wintermute.events.core.desktop import WINDOW_SPAN_STATE_KEY
from wintermute.events.core.presentation import body_text, origin_text, timestamp_text
from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.types import AudioSliceData, EventRecord, Identifier, _Record


class ActivityItem(_Record):
    """activity_key 标识比较对象，content 是该对象此次报告的内容。"""

    activity_id: Identifier
    user_id: Identifier
    conversation_id: Identifier | None
    terminal_id: Identifier
    source_id: Identifier
    kind: Literal["perception", "slice"]
    activity_key: str
    content: JsonValue
    event_type: str
    occurred_at: AwareDatetime
    known_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    source_event_ids: tuple[Identifier, ...]


def _key(*parts: object) -> str:
    # 分隔符或 Unicode 出现在终端 ID、状态键、文件名中也不会混淆身份。
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def perception_activity(event: EventRecord) -> ActivityItem:
    payload = event.payload
    if event.type == "state.observed":
        content: JsonValue = {key: payload[key] for key in ("subject", "state_key", "value")}
    else:
        content = {key: value for key, value in payload.items() if key != "valid_until"}
    expiry = payload.get("valid_until")
    return ActivityItem(
        activity_id=event.event_id,
        user_id=event.user_id,
        conversation_id=event.conversation_id,
        terminal_id=event.terminal_id,
        source_id=event.source_id,
        kind="perception",
        activity_key=_key(
            "perception",
            event.conversation_id,
            event.terminal_id,
            event.source_id,
            event.type,
            payload.get("subject"),
            payload.get("state_key"),
        ),
        content=content,
        event_type=event.type,
        occurred_at=event.occurred_at,
        known_at=event.received_at,
        valid_until=datetime.fromisoformat(expiry) if isinstance(expiry, str) else None,
        source_event_ids=(event.event_id,),
    )


def slice_activity(
    item: SliceStreamItem, sources: Sequence[EventRecord], *, known_at: datetime
) -> ActivityItem:
    ended_at = item.ended_at
    if len(sources) == 1 and sources[0].type == "slice.audio":
        audio = AudioSliceData.model_validate(sources[0].payload["audio"])
        group: tuple[object, ...] = ("audio", audio.recording_id, audio.sequence)
        ended_at = item.started_at + timedelta(seconds=audio.samples / audio.sample_rate)
    else:
        group = ("events", sorted(item.source_event_ids))
    return ActivityItem(
        activity_id=item.item_id,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        terminal_id=item.terminal_id,
        source_id=item.source_id,
        kind="slice",
        activity_key=_key("slice", item.conversation_id, item.terminal_id, item.source_id, *group),
        content={"text": item.text},
        event_type="slice.summary",
        occurred_at=item.started_at,
        ended_at=ended_at,
        known_at=known_at,
        source_event_ids=item.source_event_ids,
    )


def _state_text(key: str, value: JsonValue) -> str:
    if key == WINDOW_SPAN_STATE_KEY and isinstance(value, dict):
        app, title = value.get("foreground_app"), value.get("window_title")
        start, end = value.get("started_at"), value.get("ended_at")
        seconds = value.get("duration_seconds")
        if (
            isinstance(app, str)
            and isinstance(start, str)
            and isinstance(end, str)
            and type(seconds) is int
            and seconds > 0
        ):
            window = f"，窗口标题「{title}」" if isinstance(title, str) and title else ""
            return (
                f"桌面前台停留（采样估算）：{start} 至 {end}，约 {seconds} 秒。"
                f"应用「{app}」{window}。"
            )
    return f"{key}：{body_text(value)}"


def activity_message(item: ActivityItem) -> UserMessage:
    timestamp = timestamp_text(item.occurred_at)
    if item.ended_at and item.ended_at != item.occurred_at:
        timestamp += " 至 " + timestamp_text(item.ended_at)
    if item.valid_until is not None:
        timestamp += "（有效至 " + timestamp_text(item.valid_until) + "）"
    if (item.ended_at or item.occurred_at) > item.known_at:
        timestamp += "（来源报告的时间晚于接收时间，待确认）"
    content = item.content
    if item.kind == "slice" and isinstance(content, dict):
        # 单条规则摘要中首行已有与时间字段完全相同的前缀。
        prefix = item.occurred_at.isoformat(timespec="seconds") + " "
        text = body_text(content["text"])
        if item.ended_at == item.occurred_at and len(item.source_event_ids) == 1:
            text = text.removeprefix(prefix)
        source = "录音" if item.source_id.startswith("recording:") else item.source_id
        origin = origin_text("活动更新 · 切片", item.terminal_id, source)
    else:
        origin = origin_text("活动更新 · 感知", item.terminal_id, item.source_id)
        if item.event_type == "state.observed" and isinstance(content, dict):
            subject = content.get("subject")
            text = _state_text(str(content["state_key"]), content["value"])
            if subject and subject != f"device:{item.terminal_id}":
                text = f"{subject} · {text}"
        else:
            text = f"{item.event_type}：{body_text(content)}"
    return UserMessage(text, "timeline", timestamp, origin)


def timeline_changes(
    records: Iterable[tuple[ActivityItem, bool]],
) -> tuple[list[ActivityItem], bool]:
    """对比可见活动与已送达版本，保留迟到和修订发生后的有序后缀。"""
    previous: dict[str, UserMessage] = {}
    current: dict[str, ActivityItem] = {}
    for item, applied in records:
        key = item.activity_key if item.kind == "slice" else item.activity_id
        current[key] = item
        if applied:
            previous[key] = activity_message(item)
    old = list(previous.values())
    new = list(current.values())
    start = next(
        (i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != activity_message(b)),
        len(old),
    )
    return new[start:], start < len(old)


def timeline_lines(items: Iterable[ActivityItem]) -> Iterable[str]:
    for item in items:
        entry = activity_message(item)
        source = str(entry.origin).removeprefix("活动更新 · ")
        body = entry.text.replace("\n", "\n    ")
        yield f"{entry.timestamp} | {source} | {body}"


REVISION_NOTE = "从以下第一项起，按这段更新后的顺序读取。"


def timeline_delta(records: Iterable[tuple[ActivityItem, bool]]) -> UserMessage | None:
    changed, revision = timeline_changes(records)
    if not changed:
        return None
    lines = list(timeline_lines(changed))
    if revision:
        lines.insert(0, REVISION_NOTE)
    return UserMessage(
        "\n".join(lines), "timeline", activity_message(changed[-1]).timestamp, "活动时间线"
    )
