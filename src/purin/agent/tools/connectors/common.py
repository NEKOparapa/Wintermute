"""Shared connector tool arguments and JSON results."""

import json
from collections.abc import Mapping
from typing import Annotated

from pydantic import Field

from purin.agent import ToolResult

type NonEmptyStr = Annotated[str, Field(min_length=1, max_length=256)]


def json_result(data: Mapping[str, object], *, is_error: bool = False) -> ToolResult:
    return ToolResult(json.dumps(data, ensure_ascii=False), is_error=is_error)
