"""Filter short desktop visits and titles, keeping each surviving observation in order."""

from datetime import datetime
from typing import Literal, TypedDict

from pydantic import JsonValue

from wintermute.events.core.activity import (
    ActivityItem,
    desktop_visual_activity,
    perception_activity,
    timeline_line,
)
from wintermute.events.core.desktop import WINDOW_SPAN_STATE_KEY
from wintermute.events.core.types import EventRecord

SHORT_DWELL_SECONDS = 8


def is_wintermute_activity(state_key: object, value: object) -> bool:
    # The mobile client uses this state exclusively for its own foreground page / background.
    if state_key == "mobile.app_activity":
        return True
    if state_key not in (WINDOW_SPAN_STATE_KEY, "desktop.activity") or not isinstance(value, dict):
        return False
    name = value.get("foreground_app")
    return isinstance(name, str) and name.strip().casefold() in {
        "wintermute",
        "wintermute.exe",
        "wintermute.app",
        "dev.wintermute.desktop",
    }


def _time(value: JsonValue) -> datetime | None:
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            return stamp if stamp.tzinfo is not None else None
        except ValueError:
            pass
    return None


class RuleDecision(TypedDict):
    kind: Literal["application", "title"]
    label: str
    seconds: float
    kept: bool


def filter_perception(
    item: ActivityItem,
    decisions: list[RuleDecision] | None = None,
    *,
    threshold: int = SHORT_DWELL_SECONDS,
    deduct_short: bool = True,
) -> ActivityItem | None:
    content = item.content
    if not (
        isinstance(content, dict)
        and content.get("state_key") == WINDOW_SPAN_STATE_KEY
        and isinstance(value := content.get("value"), dict)
    ):
        return item
    filtered = filter_window(value, decisions, threshold=threshold, deduct_short=deduct_short)
    if filtered is None:
        return None
    if filtered is value:
        return item
    return item.model_copy(update={"content": {**content, "value": filtered}})


def _input_times(
    value: dict[str, JsonValue], start: datetime | None, end: datetime | None
) -> list[tuple[datetime, datetime]]:
    rows = value.get("input_segments")
    if not isinstance(rows, list) or not rows:
        return []
    result: list[tuple[datetime, datetime]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        at = _time(row.get("occurred_at"))
        if at is None:
            continue
        if start is not None and end is not None and not start <= at <= end:
            continue
        finish = _time(row.get("ended_at"))
        if finish is None or finish < at or (end is not None and finish > end):
            finish = at
        result.append((at, finish))
    return result


def filter_window(
    value: dict[str, JsonValue],
    decisions: list[RuleDecision] | None = None,
    *,
    threshold: int = SHORT_DWELL_SECONDS,
    deduct_short: bool = True,
) -> dict[str, JsonValue] | None:
    end = _time(value.get("ended_at"))
    start = _time(value.get("started_at"))
    inputs = _input_times(value, start, end)
    has_inputs = bool(inputs)
    duration = value.get("duration_seconds")
    seconds_active = (
        duration
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0
        else None
    )
    if seconds_active is not None and decisions is not None:
        decisions.append(
            RuleDecision(
                kind="application",
                label=str(value.get("foreground_app") or "应用"),
                seconds=seconds_active,
                kept=seconds_active > threshold or has_inputs,
            )
        )
    original = (
        None
        if seconds_active is not None and seconds_active <= threshold and not has_inputs
        else value
    )
    objects = value.get("work_objects")
    if not isinstance(objects, list) or not objects or start is None or end is None:
        return original
    changes: list[tuple[datetime, dict[str, JsonValue]]] = []
    for row in objects:
        if not isinstance(row, dict) or not isinstance(row.get("window_title"), str):
            return original
        at = _time(row.get("occurred_at"))
        if at is None or not start <= at <= end or (changes and at < changes[-1][0]):
            return original
        if (
            not changes
            or changes[-1][1].get("ended_at") is not None
            or changes[-1][1]["window_title"] != row["window_title"]
        ):
            changes.append((at, row))
    kept: list[JsonValue] = []
    short = []
    for index, (at, row) in enumerate(changes):
        until = changes[index + 1][0] if index + 1 < len(changes) else end
        if row.get("ended_at") is not None:
            finish = _time(row.get("ended_at"))
            if finish is None or not at <= finish <= until:
                return original
            until = finish
        seconds = (until - at).total_seconds()
        has_title_input = any(
            seg_start <= until and seg_end >= at for seg_start, seg_end in inputs
        )
        kept_title = seconds > threshold or has_title_input
        if decisions is not None:
            decisions.append(
                RuleDecision(
                    kind="title",
                    label=str(row["window_title"]),
                    seconds=seconds,
                    kept=kept_title,
                )
            )
        if not kept_title:
            short.append((at, until, row["window_title"]))
        else:
            kept.append(row)
    cleaned = dict(value)
    if original is None:
        return None
    if deduct_short and short and seconds_active is not None:
        removed = sum((until - at).total_seconds() for at, until, _ in short)
        remaining = max(0.0, round(seconds_active - removed, 6))
        if remaining < threshold and not has_inputs:
            return None
        if remaining > 0 or not has_inputs:
            cleaned["duration_seconds"] = remaining
    cleaned["work_objects"] = kept
    # A final title lasting two seconds must not reappear as the visit's fallback title.
    cleaned.pop("window_title", None)
    visuals = value.get("visual_observations")
    if isinstance(visuals, list) and short:
        # Keep the observed fact itself; suppress only the short-lived title label.
        cleaned["visual_observations"] = [
            {key: val for key, val in v.items() if key != "window_title"}
            if (
                isinstance(v, dict)
                and (when := _time(v.get("occurred_at"))) is not None
                and any(a <= when <= b and title == v.get("window_title") for a, b, title in short)
            )
            else v
            for v in visuals
        ]
    return cleaned


def perception_line(
    item: ActivityItem,
    visual_source: EventRecord | None = None,
    *,
    threshold: int = SHORT_DWELL_SECONDS,
    deduct_short: bool = True,
) -> str:
    if visual_source is not None:
        filtered = filter_perception(
            perception_activity(visual_source), threshold=threshold, deduct_short=deduct_short
        )
        if filtered is None:
            return ""
        assert isinstance(filtered.content, dict)
        value = filtered.content["value"]
        assert isinstance(value, dict)
        if not value.get("visual_observations"):
            return ""
        event = visual_source.model_copy(
            update={"payload": {**visual_source.payload, "value": value}}
        )
        return timeline_line(desktop_visual_activity(event, item.known_at))
    filtered = filter_perception(item, threshold=threshold, deduct_short=deduct_short)
    return timeline_line(filtered) if filtered else ""
