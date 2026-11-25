"""Desktop foreground visit observations."""

from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from pydantic import JsonValue

WINDOW_SPAN_STATE_KEY = "desktop.window_span"


def compact_window_title(title: str, app: object) -> str:
    """Remove Chrome's redundant window-title suffix from rendered activity text."""
    if isinstance(app, str) and app.strip().casefold() in {
        "chrome",
        "chrome.exe",
        "google chrome",
        "google chrome.app",
        "com.google.chrome",
    }:
        return title.rstrip().removesuffix(" - Google Chrome") or title
    return title


def observation_time(value: JsonValue) -> datetime | None:
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is not None:
                return stamp
        except ValueError:
            pass
    return None


def _one_line(value: JsonValue) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


@dataclass
class _VisitRow:
    at: datetime
    title: str
    until: datetime | None = None
    texts: list[str] = field(default_factory=list)

    def covers(self, at: datetime, visit_end: datetime) -> bool:
        return self.until is None or at < self.until or at == self.until == visit_end


def visit_rows(
    value: dict[str, JsonValue], start: datetime, end: datetime, app: str, zone: tzinfo
) -> list[str]:
    def observations(key: str) -> list[tuple[datetime, dict[str, JsonValue]]]:
        entries = value.get(key)
        if not isinstance(entries, list):
            return []
        return sorted(
            (
                (at, row)
                for row in entries
                if isinstance(row, dict)
                and (at := observation_time(row.get("occurred_at"))) is not None
                and start <= at <= end
            ),
            key=lambda entry: entry[0],
        )

    windows = [
        _VisitRow(at, compact_window_title(title, app), observation_time(row.get("ended_at")))
        for at, row in observations("work_objects")
        if (title := _one_line(row.get("window_title")))
    ]
    if not windows:
        title = _one_line(value.get("window_title")) or app
        windows.append(_VisitRow(start, compact_window_title(title, app)))
    rows: list[_VisitRow] = []
    next_window = 0
    inputs = observations("input_segments")
    for i, (at, segment) in enumerate(inputs):
        text = _one_line(segment.get("text"))
        if not text:
            continue
        while next_window < len(windows) and windows[next_window].at <= at:
            rows.append(windows[next_window])
            next_window += 1
        window = windows[next_window - 1] if next_window else None
        fallback = window.title if window and window.covers(at, end) else app
        title = compact_window_title(_one_line(segment.get("window_title")), app) or fallback
        # A segment can supply a title when a window switch was unavailable or omitted.
        if not rows or rows[-1].title != title or not rows[-1].covers(at, end):
            rows.append(_VisitRow(at, title))
        rows[-1].texts.append(text)
        omitted = value.get("input_segments_omitted")
        if i == 0 and len(inputs) > 1 and type(omitted) is int and omitted > 0:
            rows[-1].texts.append(f"中间略去 {omitted} 段输入")
    rows.extend(windows[next_window:])

    clock = (
        "%H:%M:%S"
        if start.astimezone(zone).date() == end.astimezone(zone).date()
        else "%m-%d %H:%M:%S"
    )
    lines = []
    for row in rows:
        line = f" - {row.at.astimezone(zone).strftime(clock)} {row.title}"
        if row.texts:
            line += " (text：" + " → ".join(row.texts) + ")"
        lines.append(line)
        omitted = value.get("work_objects_omitted")
        if row is windows[0] and len(windows) > 1 and type(omitted) is int and omitted > 0:
            lines.append(f" - 中间略去 {omitted} 次切换")
    return lines


def device_label(value: dict[str, JsonValue], device_id: str) -> str:
    platform = _one_line(value.get("device_platform"))
    name = _one_line(value.get("device_name")) or device_id
    return f"{platform} {name}" if platform else name
