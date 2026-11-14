"""Wire formats emitted by older versions of the desktop collector."""

import re
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import JsonValue

WINDOW_SPAN_STATE_KEY = "desktop.window_span"
SNAPSHOT_STATE_KEY = "desktop.activity"
_JS_TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
_WINDOW_SPAN = re.compile(
    rf"桌面前台停留（采样估算）：({_JS_TIMESTAMP}) 至 ({_JS_TIMESTAMP})，约 "
    r"([1-9][0-9]{0,11}) 秒。应用「([^\r\n]+)」。"
)


def is_desktop_snapshot(state_key: object, value: object) -> bool:
    if state_key != SNAPSHOT_STATE_KEY or not isinstance(value, dict):
        return False
    if value.get("activity") == "active":
        return value.keys() <= {"activity", "foreground_app", "window_title"} and all(
            isinstance(item, str) for item in value.values()
        )
    return value.keys() == {"activity"} and value["activity"] in ("idle", "locked", "unknown")


def legacy_window_span_value(
    producer_event_id: str, text: object, occurred_at: datetime | None
) -> dict[str, JsonValue] | None:
    if not isinstance(text, str) or occurred_at is None:
        return None
    if not (match := _WINDOW_SPAN.fullmatch(text)):
        return None
    start, end, seconds, window = match.groups()
    app, has_title, title = window.partition("」，窗口标题「")
    if not 1 <= len(app) <= 256 or (has_title and not 1 <= len(title) <= 512):
        return None
    try:
        identifier = UUID(producer_event_id)
        started_at, ended_at = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return None
    # JS Math.round rounds a positive half second up.
    rounded = (ended_at - started_at + timedelta(milliseconds=500)) // timedelta(seconds=1)
    if (
        identifier.version != 4
        or str(identifier) != producer_event_id
        or ended_at != occurred_at
        or rounded != int(seconds)
    ):
        return None
    value: dict[str, JsonValue] = {
        "foreground_app": app,
        "started_at": start,
        "ended_at": end,
        "duration_seconds": rounded,
    }
    if has_title:
        value["window_title"] = title
    return value
