"""日志格式化和敏感字段处理，不修改原始 LogRecord。"""

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from wintermute.logging.context import get_log_context

_REDACTED = "[REDACTED]"
_STANDARD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "exc_text",
}
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "proxy_authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "credential",
    "credentials",
}
_KEY_PATTERN = (
    r"api[_-]?key|authorization|proxy[_-]authorization|password|passwd|"
    r"(?:access[_-]|refresh[_-])?token|(?:client[_-])?secret|credentials?"
)
_BEARER_PATTERN = re.compile(
    r"(?i)(\b(?:proxy[_-])?authorization\b[\"']?\s*[:=]\s*)(?:Bearer|Basic)\s+[^\s,;}\]]+"
)
_FIELD_PATTERN = re.compile(
    r"(?i)(\b(?:" + _KEY_PATTERN + r")\b[\"']?\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|[^\s,;}\]]+)"
)


class Redactor:
    """按字段名和调用方提供的凭证值脱敏，包括普通文字及异常堆栈。"""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values: set[str] = set()
        for value in secrets:
            if not isinstance(value, str):
                raise TypeError("Secrets must be strings")
            if value:
                values.add(value)
                values.add(json.dumps(value, ensure_ascii=False)[1:-1])
                values.add(json.dumps(value, ensure_ascii=True)[1:-1])
                values.add(repr(value)[1:-1])
        self._secrets = tuple(sorted(values, key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, _REDACTED)
        value = _BEARER_PATTERN.sub(lambda match: match.group(1) + _REDACTED, value)
        return _FIELD_PATTERN.sub(lambda match: match.group(1) + _REDACTED, value)

    @staticmethod
    def _sensitive(key: object) -> bool:
        normalized = str(key).lower().replace("-", "_")
        return normalized in _SENSITIVE_KEYS or normalized.endswith(
            ("_api_key", "_password", "_token", "_secret")
        )

    def value(self, value: object, *, depth: int = 0) -> Any:
        if depth > 20:
            return "[maximum depth]"
        if isinstance(value, Mapping):
            return {
                self.text(str(key)): (
                    _REDACTED if self._sensitive(key) else self.value(item, depth=depth + 1)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.value(item, depth=depth + 1) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            # JSON 不允许 NaN/Infinity；日志仍可保留其文字表示。
            return value if float("-inf") < value < float("inf") else str(value)
        return self.text(str(value))


class _EventFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.redactor = Redactor(secrets)

    def event(self, record: logging.LogRecord) -> dict[str, Any]:
        data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = get_log_context()
        if context:
            data["context"] = context
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_FIELDS and not key.startswith("_")
        }
        if extra:
            data["extra"] = extra
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            data["exception"] = record.exc_text
        if record.stack_info:
            data["stack"] = self.formatStack(record.stack_info)
        result: dict[str, Any] = self.redactor.value(data)
        return result


class JSONFormatter(_EventFormatter):
    """每条记录占一行，关联字段和额外字段分别保留为结构化对象。"""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.event(record), ensure_ascii=False, allow_nan=False)


class TextFormatter(_EventFormatter):
    """适合终端阅读的 UTC 时间、级别、模块名和关联字段。"""

    def format(self, record: logging.LogRecord) -> str:
        data = self.event(record)
        result = f"{data['timestamp']} {data['level']} {data['logger']}: {data['message']}"
        for key in ("context", "extra"):
            if key in data:
                result += (
                    " " + key + "=" + json.dumps(data[key], ensure_ascii=False, allow_nan=False)
                )
        for key in ("exception", "stack"):
            if key in data:
                result += "\n" + str(data[key])
        return result
