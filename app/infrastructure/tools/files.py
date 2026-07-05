from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .base import Tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_READ_BYTES = 200_000  # 约 200KB，超过将被截断回喂


class ReadFileTool(Tool):
    """读取受限工作目录内的文本文件。"""

    name = "read_file"
    description = (
        "读取受限工作目录内的文本文件。"
        "返回 JSON 字符串，含 path、content、truncated。"
        "默认 utf-8 编码，超过 200KB 会截断。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作目录的文件路径。",
            },
            "encoding": {
                "type": "string",
                "description": "文本编码，默认 utf-8。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        workdir: Path | str,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> None:
        """绑定工作目录与最大读取字节数，必要时创建目录。"""
        self.workdir = Path(workdir)
        self.max_bytes = max_bytes
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, arguments: dict[str, Any]) -> str:
        relative = str(arguments.get("path", "")).strip()
        encoding = str(arguments.get("encoding") or "utf-8").strip() or "utf-8"

        try:
            target = _resolve_within(self.workdir, relative)
        except ValueError as exc:
            return _error(str(exc))

        if not target.exists():
            return _error("文件不存在", path=relative)
        if not target.is_file():
            return _error("路径不是文件", path=relative)

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return _error(f"读取失败: {exc}", path=relative)

        truncated = len(raw) > self.max_bytes
        if truncated:
            raw = raw[: self.max_bytes]
        try:
            content = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            return _error(f"解码失败({encoding}): {exc}", path=relative)

        logger.info(
            "read_file path=%s bytes=%s truncated=%s", relative, len(raw), truncated
        )
        return json.dumps(
            {"path": relative, "content": content, "truncated": truncated},
            ensure_ascii=False,
        )


class WriteFileTool(Tool):
    """覆盖式写入受限工作目录内的文本文件。"""

    name = "write_file"
    description = (
        "把文本写入受限工作目录内的文件，覆盖已有内容，必要时自动创建父目录。"
        "默认 utf-8 编码。返回 JSON 字符串，含 path 与 bytes_written。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作目录的文件路径。",
            },
            "content": {
                "type": "string",
                "description": "要写入的文本内容。",
            },
            "encoding": {
                "type": "string",
                "description": "文本编码，默认 utf-8。",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, *, workdir: Path | str) -> None:
        """绑定工作目录，必要时创建。"""
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, arguments: dict[str, Any]) -> str:
        relative = str(arguments.get("path", "")).strip()
        content = arguments.get("content", "")
        if not isinstance(content, str):
            return _error("content 必须是字符串")
        encoding = str(arguments.get("encoding") or "utf-8").strip() or "utf-8"

        try:
            target = _resolve_within(self.workdir, relative)
        except ValueError as exc:
            return _error(str(exc))

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode(encoding)
            target.write_bytes(data)
        except (OSError, UnicodeEncodeError, LookupError) as exc:
            return _error(f"写入失败: {exc}", path=relative)

        logger.info("write_file path=%s bytes=%s", relative, len(data))
        return json.dumps(
            {"path": relative, "bytes_written": len(data)},
            ensure_ascii=False,
        )


def _resolve_within(workdir: Path, relative: str) -> Path:
    """把 relative 解析到 workdir 内。空路径、绝对路径或越界都抛 ValueError。"""
    if not relative:
        raise ValueError("path 不能为空。")
    candidate = (workdir / relative).resolve()
    base = workdir.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path 越界工作目录: {relative}") from exc
    return candidate


def _error(message: str, **extra: Any) -> str:
    """统一的错误结果格式，与其它工具保持一致。"""
    return json.dumps({"error": message, **extra}, ensure_ascii=False)
