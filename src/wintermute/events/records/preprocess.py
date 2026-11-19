"""Conservative, deterministic noise filtering and bounded document chunks."""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from wintermute.events.storage.repositories.records import RecordContent, RecordItem

_OTP = re.compile(
    r"(?is)(?:验证码|校验码|动态码|verification\s*code|one.time\s*(?:code|password)|\bOTP\b)"
    r".{0,60}(?<!\d)\d{4,8}(?!\d)|(?<!\d)\d{4,8}(?!\d).{0,60}"
    r"(?:验证码|校验码|动态码|verification\s*code|one.time\s*code|\bOTP\b)"
)
_MARKETING = re.compile(r"退订(?:回[复復]?)?\s*[TtNn]|回[复復]\s*[TtNn]\s*退订|unsubscribe", re.I)
_PICKUP = re.compile(r"取件码|提货码|快递已(?:到|签收)|包裹已(?:到|签收)")
_BILL = re.compile(r"(?:中国移动|中国联通|中国电信).{0,60}(?:账单|余额|话费|流量)", re.S)
_TEMPLATE = re.compile(
    r"^(?:untitled|无标题|标题|请输入(?:内容|标题)|模板|todo|待填写)[.。!！\s]*$", re.I
)


@dataclass(frozen=True)
class PreprocessedRecord:
    texts: tuple[str, ...] = ()
    filtered_reason: str | None = None


def _field(data: dict[str, JsonValue], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _seconds(data: dict[str, JsonValue]) -> float:
    value = data.get("duration_seconds", data.get("duration", 0))
    return (
        max(0, float(value))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0
    )


def _chunks(text: str, max_chars: int) -> tuple[str, ...]:
    """Prefer headings/paragraph boundaries but never discard a long unbroken paragraph."""
    result: list[str] = []
    while len(text) > max_chars:
        stop = text.rfind("\n## ", 0, max_chars + 1)
        if stop < max_chars // 4:
            stop = text.rfind("\n\n", 0, max_chars + 1)
        if stop < max_chars // 4:
            stop = max_chars
        result.append(text[:stop])
        text = text[stop:]
    if text:
        result.append(text)
    return tuple(result)


def preprocess(
    item: RecordItem,
    payload: RecordContent,
    *,
    max_chars: int = 16000,
    previous: RecordContent | None = None,
) -> PreprocessedRecord:
    if max_chars < 256:
        raise ValueError("Record chunks require at least 256 characters")
    data = payload if isinstance(payload, dict) else {}
    text = (
        payload
        if isinstance(payload, str)
        else _field(data, "text", "content", "body", "markdown", "transcript", "summary")
    )
    title = item.title or _field(data, "title", "name")
    if item.record_type == "sms":
        if _OTP.search(text):
            return PreprocessedRecord(filtered_reason="verification_code")
        if _MARKETING.search(text):
            return PreprocessedRecord(filtered_reason="marketing")
        if _PICKUP.search(text):
            return PreprocessedRecord(filtered_reason="delivery_notification")
        if _BILL.search(text):
            return PreprocessedRecord(filtered_reason="operator_bill")
        if not text.strip():
            return PreprocessedRecord(filtered_reason="empty_sms")
        contact = _field(data, "contact", "sender", "phone", "number")
        direction = _field(data, "direction")
        text = f"联系人：{contact or '未知'}；方向：{direction or '未知'}\n{text}"
    elif item.record_type == "call_log":
        contact = _field(data, "contact", "name", "phone", "number")
        direction = _field(data, "direction", "type")
        duration = _seconds(data)
        spam = data.get("is_spam") is True or bool(_field(data, "spam_label"))
        if duration == 0 and direction in {"missed", "未接", "rejected", "拒接"} and spam:
            return PreprocessedRecord(filtered_reason="missed_spam_call")
        # Each card contains only facts needed for frequency/duration aggregation.
        text = f"联系人：{contact or '未知'}；通话方向：{direction}；时长秒：{duration:g}"
    elif item.record_type == "contacts":
        names = ("name", "display_name", "notes", "note", "remark")
        if not any(_field(data, key) for key in names):
            return PreprocessedRecord(filtered_reason="unnamed_contact")
        if previous == payload:
            return PreprocessedRecord(filtered_reason="unchanged_contact")
        changed = data
        if isinstance(previous, dict):
            changed = {key: value for key, value in data.items() if previous.get(key) != value}
            removed = [key for key in previous if key not in data]
            if removed:
                changed["removed_fields"] = list(removed)
            if not changed:
                return PreprocessedRecord(filtered_reason="unchanged_contact")
        text = json.dumps(changed, ensure_ascii=False, sort_keys=True)
    elif item.record_type == "recording_list":
        if not title and not text.strip() and _seconds(data) < 5:
            return PreprocessedRecord(filtered_reason="empty_short_recording")
        text = f"时长秒：{_seconds(data):g}\n{text}"
    else:
        if not text.strip():
            return PreprocessedRecord(filtered_reason="empty_document")
        if item.record_type == "notion" and _TEMPLATE.fullmatch(text.strip("# \n\r")):
            return PreprocessedRecord(filtered_reason="template_placeholder")

    path = _field(data, "path", "parent", "parent_page")
    header = (
        f"[{item.occurred_at.isoformat()} | {item.record_type}:{item.source_name}]\n"
        f"标题：{title or '无标题'}" + (f"；路径：{path}" if path else "") + "\n"
    )
    # Metadata is untrusted and can be long too; bound it independently of the body.
    header = header[: min(len(header), max_chars // 3)]
    pieces = _chunks(text, max_chars - len(header) - 40)
    return PreprocessedRecord(
        tuple(
            f"{header}分片 {index + 1}/{len(pieces)}\n{piece}" for index, piece in enumerate(pieces)
        )
    )


def aggregate(
    items: Sequence[tuple[RecordItem, str]], *, payload: Callable[[RecordItem], RecordContent]
) -> str:
    """Compact SMS by contact/day and calls by contact/month, retaining source item identities.

    A caller keeps its existing item acknowledgement window. This transformation never grows
    that window's text budget; partial large SMS fragments retain their original bounded text.
    """
    original = "\n\n".join(text for _, text in items)
    if not items:
        return original
    kind = items[0][0].record_type
    if kind not in {"sms", "call_log"} or any(item.record_type != kind for item, _ in items):
        return original
    if len({item.item_id for item, _ in items}) != len(items):
        return original
    if any(re.search(r"(?m)^分片 \d+/(?:[2-9]|\d{2,})$", text) for _, text in items):
        return original
    groups: dict[tuple[str, str, str], list[tuple[RecordItem, dict[str, JsonValue], str]]] = {}
    for item, _ in items:
        content = payload(item)
        data = content if isinstance(content, dict) else {}
        contact = _field(data, "contact", "sender", "name", "phone", "number")
        # Do not infer that distinct anonymous contacts denote the same person.
        identity = contact or "unknown:" + item.item_id
        window = item.occurred_at.strftime("%Y-%m-%d" if kind == "sms" else "%Y-%m")
        text = (
            content
            if isinstance(content, str)
            else _field(data, "text", "content", "body", "markdown", "transcript", "summary")
        )
        groups.setdefault((item.source_name, identity, window), []).append((item, data, text))

    documents: list[dict[str, JsonValue]] = []
    for (source, contact, window), members in groups.items():
        members.sort(key=lambda member: (member[0].occurred_at, member[0].item_id))
        rows: list[JsonValue] = []
        for item, data, text in members:
            row: dict[str, JsonValue] = {
                "item_id": item.item_id,
                "occurred_at": item.occurred_at.isoformat(),
                "direction": _field(data, "direction", "type"),
            }
            if item.title:
                row["title"] = item.title
            if kind == "sms":
                row["text"] = text
            else:
                row["duration_seconds"] = _seconds(data)
            rows.append(row)
        group: dict[str, JsonValue] = {
            "record_type": kind,
            "source_name": source,
            "contact": contact,
            "window_utc": window,
            "records": rows,
        }
        if kind == "call_log":
            group.update(
                {
                    "count_in_this_batch": len(members),
                    "total_duration_seconds": sum(_seconds(data) for _, data, _ in members),
                    "last_contact_at": members[-1][0].occurred_at.isoformat(),
                }
            )
        documents.append(group)
    compact = (
        "以下按联系人和UTC日期窗口聚合，仅统计本批资料；每条原始时间与来源优先。\n"
        + json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    )
    return compact if len(compact) <= len(original) else original
