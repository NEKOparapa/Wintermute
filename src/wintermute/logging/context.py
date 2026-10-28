"""在异步任务之间隔离日志关联字段。"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("wintermute-log-context", default=None)


def get_log_context() -> dict[str, Any]:
    """返回副本，调用方不能修改其他任务继承的关联字段。"""
    return deepcopy(_context.get() or {})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """临时绑定可序列化字段；嵌套覆盖，退出及异常时恢复上一层。"""
    json.dumps(fields, ensure_ascii=False, allow_nan=False)
    merged = get_log_context()
    merged.update(deepcopy(fields))
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)
