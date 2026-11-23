"""将不可变观测批次归并为规则摘要；交付时只投影预算允许的细节。"""

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wintermute.agent import UserMessage
from wintermute.agent.context.types import EstimatedTokenCounter
from wintermute.events.core.activity import (
    REVISION_NOTE,
    ActivityItem,
    activity_message,
    timeline_line,
    window_span_details,
    window_span_inputs,
)
from wintermute.events.core.desktop import WINDOW_SPAN_STATE_KEY, compact_window_title
from wintermute.events.core.presentation import timestamp_text


@dataclass(frozen=True, slots=True)
class TimelinePolicy:
    raw_tokens: int = 1500
    ai_threshold: int = 600
    summary_tokens: int = 300
    delivery_tokens: int = 800
    idle_seconds: int = 900
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        for name in (
            "raw_tokens",
            "ai_threshold",
            "summary_tokens",
            "delivery_tokens",
            "idle_seconds",
        ):
            value = getattr(self, name)
            minimum = 128 if name == "delivery_tokens" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("timezone must name an available IANA timezone") from exc


_COUNTER = EstimatedTokenCounter()


def estimate(text: str) -> int:
    return _COUNTER.count_text(text)


def clip(text: str, limit: int) -> str:
    """按同一保守预算截断，不截断 UTF-8 字符。"""
    if limit <= 0:
        return ""
    if estimate(text) <= limit:
        return text
    marker = "…" if limit >= 3 else "." * limit
    return text.encode("utf-8")[: limit - estimate(marker)].decode("utf-8", "ignore") + marker


def raw_size(item: ActivityItem) -> int:
    return estimate(timeline_line(item))


@dataclass(frozen=True, slots=True)
class BatchText:
    start: datetime
    end: datetime
    count: int
    rule_text: str
    ai_text: str | None = None
    # Storage-only statistics for deterministic rollups across batches.
    stats_text: str = ""
    device_id: str | None = None


type Segment = BatchText | tuple[Sequence[ActivityItem], str | None]


@dataclass(frozen=True, slots=True)
class _Observation:
    start: datetime
    end: datetime
    origin: str
    text: str = ""
    app: str = ""
    title: str = ""
    seconds: int = 0


def _observation(item: ActivityItem, zone: ZoneInfo) -> _Observation:
    message = activity_message(item)
    origin = str(message.origin).removeprefix("活动更新 · ")
    content = item.content
    if (
        item.kind == "perception"
        and item.event_type == "state.observed"
        and item.valid_until is None
        and item.occurred_at <= item.known_at
        and isinstance(content, dict)
        and content.get("state_key") == WINDOW_SPAN_STATE_KEY
        and content.get("subject") in (None, f"device:{item.device_id}")
        and isinstance(value := content.get("value"), dict)
    ):
        try:
            start = datetime.fromisoformat(str(value.get("started_at")))
            end = datetime.fromisoformat(str(value.get("ended_at")))
        except ValueError:
            pass
        else:
            app, title, seconds = (
                value.get("foreground_app"),
                value.get("window_title"),
                value.get("duration_seconds"),
            )
            if (
                start.tzinfo
                and end.tzinfo
                and start <= end <= item.known_at
                and isinstance(app, str)
                and app
                and isinstance(seconds, (int, float))
                and not isinstance(seconds, bool)
                and seconds > 0
            ):
                objects, visuals = window_span_details(value, start, end, zone)
                inputs = window_span_inputs(value, start, end, zone)
                return _Observation(
                    start.astimezone(zone),
                    end.astimezone(zone),
                    origin,
                    text="\n".join(line for line in (inputs, visuals, objects) if line),
                    app=app,
                    title=compact_window_title(title, app) if isinstance(title, str) else "",
                    seconds=round(seconds),
                )
    # Preserve qualifications on temporary and clock-skewed reports.
    text = message.text
    if item.valid_until is not None or (item.ended_at or item.occurred_at) > item.known_at:
        text = f"{message.timestamp} {text}"
    return _Observation(
        item.occurred_at.astimezone(zone),
        (item.ended_at or item.occurred_at).astimezone(zone),
        origin,
        text=text,
    )


def _when(start: datetime, end: datetime) -> str:
    left = start.strftime("%Y-%m-%d %H:%M:%S")
    if start.timestamp() == end.timestamp():
        return left + start.strftime(" %z")
    right = end.strftime("%H:%M:%S" if start.date() == end.date() else "%Y-%m-%d %H:%M:%S")
    if start.utcoffset() != end.utcoffset():
        left += start.strftime(" %z")
    return left + "–" + right + end.strftime(" %z")


def _days(start: datetime, end: datetime) -> str:
    left, right = start.date().isoformat(), end.date().isoformat()
    return left if left == right else f"{left}至{right}"


@dataclass(slots=True)
class _Visits:
    count: int = 0
    seconds: int = 0
    titles: dict[str, None] = field(default_factory=dict)


@dataclass(slots=True)
class _Episode:
    start: datetime
    end: datetime
    origin: str
    apps: dict[str, _Visits] = field(default_factory=dict)
    notes: list[tuple[str, int]] = field(default_factory=list)

    def add(self, obs: _Observation) -> None:
        self.end = max(self.end, obs.end, key=datetime.timestamp)
        if obs.app:
            # Order by last observation so the latest title survives bounded delivery.
            visits = self.apps.pop(obs.app, _Visits())
            visits.count += 1
            visits.seconds += obs.seconds
            if obs.title and obs.title != obs.app:
                visits.titles.pop(obs.title, None)
                visits.titles[obs.title] = None
            self.apps[obs.app] = visits
        if not obs.text:
            return
        note = f"{obs.app} · {obs.text}" if obs.app else obs.text
        if self.notes and self.notes[-1][0] == note:
            self.notes[-1] = (note, self.notes[-1][1] + 1)
        else:
            self.notes.append((note, 1))

    def render(self) -> str:
        lines = [f"{_when(self.start, self.end)} | {self.origin}"]
        for app, visits in self.apps.items():
            row = f"{app} {visits.count}次，约{visits.seconds}秒"
            if visits.titles:
                row += "；窗口：" + "、".join(reversed(visits.titles))
            lines.append(row)
        for note, count in self.notes:
            lines.append(note + (f"（{count}次）" if count > 1 else ""))
        return "\n".join(lines)


def batch_text(items: Sequence[ActivityItem], policy: TimelinePolicy) -> BatchText:
    """只加总实际报告的时长；观测间的空白绝不计作停留。"""
    if not items:
        raise ValueError("an activity batch cannot be empty")
    zone = ZoneInfo(policy.timezone)
    observations = sorted(
        (_observation(i, zone) for i in items),
        key=lambda o: (o.start.timestamp(), o.end.timestamp()),
    )
    episodes: list[_Episode] = []
    by_origin: dict[str, _Episode] = {}
    stats: dict[tuple[str, str, str], _Visits] = defaultdict(_Visits)
    for obs in observations:
        ep = by_origin.get(obs.origin)
        if (
            ep is None
            or ep.start.date() != obs.start.date()
            or obs.start.date() != obs.end.date()
            or (obs.start.timestamp() - ep.end.timestamp()) >= policy.idle_seconds
        ):
            ep = _Episode(obs.start, obs.end, obs.origin)
            by_origin[obs.origin] = ep
            episodes.append(ep)
        ep.add(obs)
        if obs.app:
            v = stats[_days(obs.start, obs.end), obs.origin, obs.app]
            v.count += 1
            v.seconds += obs.seconds
            if obs.title and obs.title != obs.app:
                v.titles[obs.title] = None
    # Ending times may interleave between devices.
    episodes.sort(key=lambda ep: ep.end.timestamp())
    latest = max(observations, key=lambda o: (o.end.timestamp(), o.start.timestamp()))
    detail = latest.text
    if latest.app:
        detail = f"{latest.app} 约{latest.seconds}秒"
        if latest.text:
            detail += "；" + latest.text
        elif latest.title and latest.title != latest.app:
            detail += f"；窗口：{latest.title}"
    # Keep a bounded semantic example to carry into older rollups, separate from usage totals.
    topic = next((obs for obs in reversed(observations) if obs.text), None)
    devices = {item.device_id for item in items}
    return BatchText(
        observations[0].start,
        latest.end,
        len(items),
        f"时区：{policy.timezone}\n" + "\n".join(ep.render() for ep in episodes),
        stats_text=json.dumps(
            {
                "usage": [(*k, v.count, v.seconds) for k, v in stats.items()],
                "windows": [[app, title] for (_, _, app), v in stats.items() for title in v.titles],
                "latest": clip(detail, policy.delivery_tokens),
                "origin": latest.origin,
                "topic": clip(topic.text, policy.summary_tokens) if topic else "",
                "topic_origin": topic.origin if topic else "",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        device_id=devices.pop() if len(devices) == 1 else None,
    )


def rule_text(items: Sequence[ActivityItem], policy: TimelinePolicy) -> str:
    return batch_text(items, policy).rule_text


def _usage(batches: Sequence[BatchText]) -> dict[tuple[str, str, str], _Visits]:
    stats: dict[tuple[str, str, str], _Visits] = defaultdict(_Visits)
    for batch in batches:
        for day, origin, app, count, seconds in json.loads(batch.stats_text or "{}").get(
            "usage", []
        ):
            visits = stats[day, origin, app]
            visits.count += count
            visits.seconds += seconds
    return stats


def merge_stats(batches: Sequence[BatchText]) -> str:
    """多批次汇总仍以事实加总，不从模型生成的文字里提取数值。"""
    if not batches:
        raise ValueError("cannot merge an empty batch group")
    ordered = sorted(batches, key=lambda b: b.end.timestamp())
    latest = json.loads(ordered[-1].stats_text or "{}")
    topic, topic_origin = next(
        (
            (facts["topic"], facts.get("topic_origin", ""))
            for b in reversed(ordered)
            if (facts := json.loads(b.stats_text or "{}")).get("topic")
        ),
        ("", ""),
    )
    return json.dumps(
        {
            "usage": [(*k, v.count, v.seconds) for k, v in _usage(ordered).items()],
            "latest": latest.get("latest", ""),
            "origin": latest.get("origin", ""),
            "topic": topic,
            "topic_origin": topic_origin,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _stats(batches: Sequence[BatchText]) -> list[str]:
    return [
        f"{day} {origin} | {app} {v.count}次，约{v.seconds}秒"
        for (day, origin, app), v in sorted(
            _usage(batches).items(),
            key=lambda kv: (-kv[1].seconds, kv[0]),
        )
    ]


def group_rules(batches: Sequence[BatchText], policy: TimelinePolicy) -> str:
    """Fold repeated desktop totals and titles across sealed leaves before requesting AI."""
    prefix = f"时区：{policy.timezone}\n"
    usage = _usage(batches)
    if any(json.loads(b.stats_text).get("topic") for b in batches if b.stats_text) or (
        sum(visits.count for visits in usage.values()) != sum(b.count for b in batches)
    ):
        # Mixed observations and enriched visits both carry facts beyond usage totals.
        # Preserve their time/source and wording for semantic compression.
        return prefix + "\n".join(b.rule_text.removeprefix(prefix) for b in batches)
    titles = dict.fromkeys(
        (app, title) for b in batches for app, title in json.loads(b.stats_text)["windows"]
    )
    zone = ZoneInfo(policy.timezone)
    interval = _when(
        min(b.start for b in batches).astimezone(zone), max(b.end for b in batches).astimezone(zone)
    )
    lines = [interval + " 内的前台停留合计", *_stats(batches)]
    lines.extend(f"{app} 窗口：{title}" for app, title in titles)
    return prefix + "\n".join(lines)


def _select_rows(rows: Sequence[str], limit: int) -> list[str]:
    """只选择能完整容纳的事实，避免截断计数或时长。"""
    result: list[str] = []
    for row in rows:
        cost = estimate(row) + bool(result)
        if cost <= limit:
            limit -= cost
            result.append(row)
    return result


def _content(batch: BatchText, zone: ZoneInfo) -> str:
    if not batch.ai_text:
        return batch.rule_text
    interval = _when(batch.start.astimezone(zone), batch.end.astimezone(zone))
    origin = json.loads(batch.stats_text or "{}").get("origin") or batch.device_id
    scope = f" · 最近来源：{origin}" if origin else ""
    return "\n".join([f"{interval} | 活动摘要{scope}", *_stats((batch,)), batch.ai_text])


def _sourced_excerpt(text: str, source: str | None, limit: int) -> str:
    if source:
        # A short observation leaves more of the available space for a long device name.
        source_budget = limit - len(" | ") - min(estimate(text), limit // 2)
        text = f"{clip(source, source_budget)} | {text}"
    return clip(text, limit)


def _overview(batches: Sequence[BatchText], zone: ZoneInfo, limit: int) -> str:
    start = min((b.start for b in batches), key=datetime.timestamp).astimezone(zone)
    end = max((b.end for b in batches), key=datetime.timestamp).astimezone(zone)
    count = sum(b.count for b in batches)
    title = f"较早：{_when(start, end)}，共{count}条观测"
    if estimate(title) + 45 > limit:
        title = f"较早{_days(start, end)}：{count}条"
    free = limit - estimate(title) - 1
    rows: list[str] = []
    for batch in reversed(batches):
        facts = json.loads(batch.stats_text or "{}")
        topic = batch.ai_text or facts.get("topic") or ""
        if topic and free >= 24:
            source = batch.device_id or (None if batch.ai_text else facts.get("topic_origin"))
            rows.append(_sourced_excerpt(topic, source, max(24, free // 2)))
            free -= estimate(rows[0]) + 1
            break
    rows.extend(_select_rows(_stats(batches), free))
    return clip("\n".join([title, *rows]), limit)


def _recent(batch: BatchText, zone: ZoneInfo, limit: int) -> str:
    full = _content(batch, zone)
    if estimate(full) <= limit:
        return full
    facts = json.loads(batch.stats_text or "{}")
    detail = facts.get("latest") or batch.ai_text or full
    device = batch.device_id or facts.get("origin")
    if limit < 96:
        return _sourced_excerpt(detail, device, limit)
    prefix = f"本段{batch.count}条；最近：{facts.get('origin') or device or '设备观测'} | "
    if estimate(prefix) <= limit // 3:
        latest = prefix + clip(detail, limit // 2)
    else:
        latest = _sourced_excerpt(detail, device, limit // 3 + limit // 2)
    free = limit - estimate(latest) - 1
    rows = _select_rows(_stats((batch,)), free)
    topic = batch.ai_text or facts.get("topic")
    if topic and topic != detail:
        free -= sum(estimate(row) + 1 for row in rows)
        if free >= 24:
            source = batch.device_id or (None if batch.ai_text else facts.get("topic_origin"))
            rows.append(_sourced_excerpt(f"摘要：{topic}", source, free))
    return "\n".join([*rows, latest])


def render_timeline(
    segments: Sequence[Segment],
    policy: TimelinePolicy,
    *,
    revision: bool = False,
) -> UserMessage | None:
    """小增量原样投递；大增量保留覆盖范围，优先分配最近观测的正文。"""
    if not segments:
        return None
    note = REVISION_NOTE + "\n" if revision else ""
    raw_lines = [note.rstrip("\n")] if revision else []
    size = estimate(note)
    last: ActivityItem | None = None
    for segment in segments:
        if isinstance(segment, BatchText) or segment[1]:
            break
        for item in segment[0]:
            line = timeline_line(item)
            size += estimate(line) + 1
            if size > policy.delivery_tokens:
                break
            raw_lines.append(line)
            last = item
        else:
            continue
        break
    else:
        if last is None:
            return None
        message = UserMessage(
            "\n".join(raw_lines), "timeline", activity_message(last).timestamp, "活动时间线"
        )
        if estimate(message.model_text()) <= policy.delivery_tokens:
            return message
    zone = ZoneInfo(policy.timezone)
    batches = []
    for segment in segments:
        if isinstance(segment, BatchText):
            batches.append(segment)
        elif segment[0]:
            b = batch_text(segment[0], policy)
            batches.append(replace(b, ai_text=segment[1]))
    if not batches:
        return None
    batches.sort(key=lambda b: b.end.timestamp())
    stamp = timestamp_text(batches[-1].end)
    if (
        revision
        and estimate(UserMessage(note, "timeline", stamp, "活动时间线").model_text()) + 48
        > policy.delivery_tokens
    ):
        note = "顺序修订：\n"

    def message_for(body: str) -> UserMessage:
        return UserMessage(note + body, "timeline", stamp, "活动时间线")

    budget = policy.delivery_tokens - estimate(message_for("").model_text())
    tail: list[str] = []
    used = 0
    for batch in reversed(batches):
        content = _content(batch, zone)
        used += estimate(content) + bool(tail)
        if used > budget:
            break
        tail.append(content)
    else:
        return message_for("\n".join(reversed(tail)))

    if len(batches) == 1:
        return message_for(_recent(batches[-1], zone, budget))
    # Reserve a short summary for everything we omit; spend remaining space from newest
    # backwards. The count and stats below include only omitted batches.
    if budget < 72:
        return message_for(_recent(batches[-1], zone, budget))
    prior_budget = min(budget // 2, max(78, budget // 3))
    remaining = budget - prior_budget - 1
    tail = [_recent(batches[-1], zone, remaining)]
    remaining -= estimate(tail[0])
    split = len(batches) - 1
    while split > 1:
        prev = _content(batches[split - 1], zone)
        if estimate(prev) + 1 > remaining:
            break
        split -= 1
        tail.append(prev)
        remaining -= estimate(prev) + 1
    prior = _overview(batches[:split], zone, prior_budget + remaining)
    return message_for("\n".join([prior, *reversed(tail)]))
