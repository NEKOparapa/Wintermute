"""将设备观测和已完成的切片投影成可比较、有来源的会话消息。"""

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, tzinfo
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


def window_span_details(
    value: dict[str, JsonValue], start: datetime, end: datetime, zone: tzinfo
) -> tuple[str, str]:
    """Render only timestamped observations belonging to this visit."""
    time_format = "%H:%M:%S"
    if start.astimezone(zone).date() != end.astimezone(zone).date():
        time_format = "%m-%d %H:%M:%S"

    def entries(key: str, field: str) -> list[str]:
        rows = value.get(key)
        results: list[str] = []
        if not isinstance(rows, list):
            return results
        for row in rows:
            if not isinstance(row, dict) or not isinstance(stamp := row.get("occurred_at"), str):
                continue
            description = row.get(field)
            if not isinstance(description, str) or not description.strip():
                continue
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when.tzinfo is not None and start <= when <= end:
                label = when.astimezone(zone).strftime(time_format)
                title = row.get("window_title")
                if key == "visual_observations" and isinstance(title, str) and title:
                    label += f"「{title}」"
                results.append(f"{label} {description.strip()}")
        return results

    objects = entries("work_objects", "window_title")
    omitted = value.get("work_objects_omitted")
    if len(objects) > 1 and type(omitted) is int and omitted > 0:
        objects.insert(1, f"中间略去 {omitted} 次切换")
    visuals = entries("visual_observations", "text")
    return (
        "工作对象：" + " → ".join(objects) if objects else "",
        "画面记录：" + "；".join(visuals) if visuals else "",
    )


def desktop_visual_activity(event: EventRecord, known_at: datetime) -> ActivityItem:
    """New text from an existing visit, dated by the screenshots rather than the model call."""
    value, frames = event.payload["value"], event.payload["frames"]
    assert isinstance(value, dict) and isinstance(frames, list)
    first, last = frames[0], frames[-1]
    assert isinstance(first, dict) and isinstance(last, dict)
    start = datetime.fromisoformat(str(first["occurred_at"]))
    end = datetime.fromisoformat(str(last["occurred_at"]))
    assert start.tzinfo is not None
    _, visuals = window_span_details(value, start, end, start.tzinfo)
    return perception_activity(event).model_copy(
        update={
            "activity_id": event.event_id + ":visual",
            "activity_key": _key("desktop.visual", event.event_id),
            "event_type": "desktop.visual",
            "occurred_at": start,
            "ended_at": end,
            "known_at": known_at,
            "content": f"应用「{value['foreground_app']}」\n{visuals}",
        }
    )


def _window_span(content: dict[str, JsonValue]) -> tuple[str, datetime] | None:
    value = content.get("value")
    if content.get("state_key") != WINDOW_SPAN_STATE_KEY or not isinstance(value, dict):
        return None
    app, title = value.get("foreground_app"), value.get("window_title")
    start, end = value.get("started_at"), value.get("ended_at")
    seconds = value.get("duration_seconds")
    if not (
        isinstance(app, str)
        and app
        and isinstance(start, str)
        and isinstance(end, str)
        and isinstance(seconds, (float, int))
        and not isinstance(seconds, bool)
        and seconds >= 0
    ):
        return None
    try:
        started, ended = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return None
    if started.tzinfo is None or ended.tzinfo is None or started > ended:
        return None
    objects, visuals = window_span_details(value, started, ended, started.tzinfo)
    window = f"，窗口标题「{title}」" if not objects and isinstance(title, str) and title else ""
    body = f"{start} 至 {end}，约 {seconds} 秒。应用「{app}」{window}。"
    return "\n".join(line for line in (body, objects, visuals) if line), ended


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
            span = _window_span(content)
            text = span[0] if span else f"{content['state_key']}：{body_text(content['value'])}"
            if subject and subject != f"device:{item.terminal_id}":
                text = f"{subject} · {text}"
        elif item.event_type == "desktop.visual" and isinstance(content, str):
            text = content
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


def timeline_line(item: ActivityItem) -> str:
    content = item.content
    if (
        item.kind == "perception"
        and item.event_type == "state.observed"
        and isinstance(content, dict)
        and content.get("subject") in (None, f"device:{item.terminal_id}")
        and item.source_id == "default"
        and item.valid_until is None
        and item.occurred_at <= item.known_at
    ):
        span = _window_span(content)
        # A normal desktop visit is reported at its own end time. The body already dates it.
        if span is not None and span[1] == item.occurred_at:
            return f"{span[0]} | 来源设备：{item.terminal_id}".replace("\n", "\n    ")
    entry = activity_message(item)
    source = str(entry.origin).removeprefix("活动更新 · ")
    body = entry.text.replace("\n", "\n    ")
    return f"{entry.timestamp} | {source} | {body}"


def timeline_lines(items: Iterable[ActivityItem]) -> Iterable[str]:
    return map(timeline_line, items)


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
