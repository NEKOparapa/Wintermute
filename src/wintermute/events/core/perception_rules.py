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
    item: ActivityItem, decisions: list[RuleDecision] | None = None
) -> ActivityItem | None:
    content = item.content
    if not (
        isinstance(content, dict)
        and content.get("state_key") == WINDOW_SPAN_STATE_KEY
        and isinstance(value := content.get("value"), dict)
    ):
        return item
    duration = value.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        if decisions is not None:
            decisions.append(
                RuleDecision(
                    kind="application",
                    label=str(value.get("foreground_app") or "应用"),
                    seconds=duration,
                    kept=duration > 3,
                )
            )
        if duration <= 3:
            return None
    objects = value.get("work_objects")
    end = _time(value.get("ended_at"))
    start = _time(value.get("started_at"))
    if not isinstance(objects, list) or not objects or start is None or end is None:
        return item
    changes: list[tuple[datetime, dict[str, JsonValue]]] = []
    for row in objects:
        if not isinstance(row, dict) or not isinstance(row.get("window_title"), str):
            return item
        at = _time(row.get("occurred_at"))
        if at is None or not start <= at <= end or (changes and at < changes[-1][0]):
            return item
        if not changes or changes[-1][1]["window_title"] != row["window_title"]:
            changes.append((at, row))
    kept: list[JsonValue] = []
    short = []
    for index, (at, row) in enumerate(changes):
        until = changes[index + 1][0] if index + 1 < len(changes) else end
        seconds = (until - at).total_seconds()
        if decisions is not None:
            decisions.append(
                RuleDecision(
                    kind="title", label=str(row["window_title"]), seconds=seconds, kept=seconds > 3
                )
            )
        if seconds <= 3:
            short.append((at, until, row["window_title"]))
        else:
            kept.append(row)
    cleaned = dict(value)
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
    return item.model_copy(update={"content": {**content, "value": cleaned}})


def perception_line(item: ActivityItem, visual_source: EventRecord | None = None) -> str:
    if visual_source is not None:
        filtered = filter_perception(perception_activity(visual_source))
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
    filtered = filter_perception(item)
    return timeline_line(filtered) if filtered else ""
