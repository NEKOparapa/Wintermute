"""将来源、时间和事件正文转为模型可读文本，审计字段保留在事件存储。"""

import json
from datetime import UTC, datetime

from pydantic import JsonValue

_AUDIT_KEYS = {
    "id",
    "activity_id",
    "activity_key",
    "event_id",
    "producer_event_id",
    "correlation_id",
    "conversation_id",
    "user_id",
    "device_id",
    "source_id",
    "source_event_ids",
    "source_event_id",
    "task_id",
    "call_id",
    "tool_call_id",
    "item_id",
    "run_id",
    "job_id",
    "output_id",
    "trace_id",
    "span_id",
    "request_id",
    "recording_id",
    "source_seq",
    "received_at",
    "known_at",
    "occurred_at",
    "created_at",
    "schema_version",
}


def timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def origin_text(category: str, device_id: str, source: str, detail: str | None = None) -> str:
    parts = [category]
    device_id = "后台服务" if device_id.startswith("wintermute:internal:") else device_id
    for name in (device_id, source, detail):
        if name and name != "default" and name not in parts:
            parts.append(name)
    return " · ".join(parts)


def body_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {k: body_value(v) for k, v in value.items() if k.casefold() not in _AUDIT_KEYS}
    if isinstance(value, list):
        return [body_value(v) for v in value]
    return value


def body_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(body_value(value), ensure_ascii=False, separators=(",", ":"))
