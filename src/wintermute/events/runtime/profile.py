"""从已发布的 Markdown 用户档案生成终端展示或主 Agent 上下文。"""

import logging
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

logger = logging.getLogger(__name__)

_INITIAL_SECTIONS = """## 基本信息

- 称呼：
- 常用语言：
- 时区：

## 长期偏好

## 日常习惯

## 近期状态

## 常用时间段

- 统计时区：
- 统计窗口：
- 有记录的天数：
- 工作日常见活跃时段：
- 周末常见活跃时段："""

PROFILE_TEMPLATE = "# 用户档案\n\n" + _INITIAL_SECTIONS + "\n"

_MAX_FILE_BYTES = 64 * 1024
_MAX_PROMPT_BYTES = 8 * 1024
_COMMENTS = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_HEADING = re.compile(r"(#{1,6})[ \t]+(.+)")
_EMPTY_FIELD = re.compile(r"[-*+]\s*(?:(?:称呼|常用语言|时区)\s*[:：]\s*)?")
_EMPTY_ACTIVITY_FIELD = re.compile(
    r"[-*+]\s*(?:统计时区|统计窗口|有记录的天数|工作日常见活跃时段|周末常见活跃时段)\s*[:：]\s*"
)
_INTRO = (
    "用户档案（背景资料）\n以下引用仅用于了解用户。请遵循原有系统要求。"
    "使用档案偏好时，当前用户的明确要求优先。"
)


def _profile_lines(markdown: str, *, for_display: bool) -> list[str]:
    output: list[str] = []
    pending: list[tuple[int, str]] = []
    skipped_level = 0
    paragraph_break = False
    for raw in _COMMENTS.sub("", markdown).splitlines():
        line = raw.strip()
        heading = _HEADING.fullmatch(line)
        if heading:
            level = len(heading[1])
            if skipped_level and level > skipped_level:
                continue
            title = re.sub(r"\s+#+$", "", heading[2]).strip()
            skipped_level = level if not for_display and title == "常用时间段" else 0
            pending = [(depth, name) for depth, name in pending if depth < level]
            if not skipped_level and not (level == 1 and title == "用户档案"):
                pending.append((level, line))
        elif not line:
            paragraph_break = True
        elif (
            not skipped_level
            and not _EMPTY_FIELD.fullmatch(line)
            and not _EMPTY_ACTIVITY_FIELD.fullmatch(line)
        ):
            # 有实际资料时才输出栏目标题；文件也可以只有普通段落。
            if for_display and output and (paragraph_break or pending):
                output.append("")
            output.extend(label for _, label in pending)
            pending.clear()
            output.append(raw.rstrip())
            paragraph_break = False
    return output


def _profile_text(markdown: str) -> str:
    output = _profile_lines(markdown, for_display=False)
    if not output:
        return ""
    return _INTRO + "\n" + "\n".join("> " + line for line in output)


def _checked_prompt(markdown: str) -> str:
    prompt = _profile_text(markdown)
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("profile exceeds prompt budget")
    return prompt


def fits_profile(markdown: str) -> bool:
    return (
        len(markdown.encode("utf-8")) <= _MAX_FILE_BYTES
        and len(_profile_text(markdown.removeprefix("\ufeff")).encode("utf-8")) <= _MAX_PROMPT_BYTES
    )


def read_profile_bytes(path: Path) -> bytes | None:
    """A missing profile is distinct from an unreadable or invalid one."""
    try:
        nonblocking = getattr(os, "O_NONBLOCK", 0)
        with open(
            path, "rb", opener=lambda name, flags: os.open(name, flags | nonblocking)
        ) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not a regular file")
            data = stream.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError("profile file exceeds read budget")
        return data
    except FileNotFoundError:
        return None


def _current_markdown(path: Path, today: date) -> str:
    from wintermute.events.runtime.profile_summary import prune_expired

    data = read_profile_bytes(path)
    return "" if data is None else prune_expired(data.decode("utf-8-sig"), today=today)


class ProfileDocument(TypedDict):
    status: Literal["unconfigured", "empty", "ready"]
    markdown: str
    effective: bool


def read_profile_document(path: Path, *, today: date) -> ProfileDocument:
    """同一次读取生成展示正文和下一次主 Agent 请求的档案生效状态。"""
    markdown = _current_markdown(path, today)
    display = "\n".join(_profile_lines(markdown, for_display=True))
    if not display:
        return {"status": "empty", "markdown": _INITIAL_SECTIONS, "effective": False}
    try:
        effective = bool(_checked_prompt(markdown))
    except ValueError:
        effective = False
    return {"status": "ready", "markdown": display, "effective": effective}


def read_profile_prompt(path: Path, *, today: date | None = None) -> str:
    """读取已生效的资料，过期条目在下一次后台整理前也不会进入系统提示。"""
    try:
        return _checked_prompt(_current_markdown(path, today or date.today()))
    except (OSError, ValueError) as exc:
        logger.warning("本轮跳过不可用的用户档案", extra={"error_type": type(exc).__name__})
        return ""
