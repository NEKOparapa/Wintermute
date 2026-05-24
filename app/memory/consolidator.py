from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Protocol

from .memory import MemoryEntry, MemoryKind
from .tokens import TokenCounter

logger = logging.getLogger(__name__)


class CompressLLM(Protocol):
    """压缩器需要的最小 LLM 接口,和 OpenAICompatibleLLM.complete 兼容。"""

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str: ...


# 会话内压缩使用的指令。强调"留事实/决定/未解",去掉客套。
_SESSION_COMPRESS_SYSTEM = """你是对话压缩助手。把下面正在进行的对话压缩成简短的记忆要点,供后续对话调取。

保留:
- 用户当前的话题、目标、偏好
- 已经确认的事实、决定、承诺
- 未解决的问题、悬而未决的事项
- 明显且对后续有用的情绪信号

去掉:
- 客套和重复确认
- 没有信息量的口水话

直接输出要点本身,不要前言后语,不要解释你做了什么。控制在 200~400 字。
"""


class Consolidator:
    """记忆压缩器:把一批源数据交给 LLM,得到一条更高层级的记忆。

    本片 (slice 2) 只实现 ``compress_to_session``;daily/weekly/monthly 留给 slice 3。
    """

    def __init__(self, llm: CompressLLM, token_counter: TokenCounter) -> None:
        """注入 LLM 客户端和 token 计数器,便于测试时替换成假实现。"""
        self.llm = llm
        self.token_counter = token_counter

    # ------------------------------------------------------------------ session

    def compress_to_session(
        self,
        events: list[dict[str, Any]],
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> MemoryEntry:
        """把一批原始事件压缩成一条 session 记忆条目。"""
        if not events:
            raise ValueError("compress_to_session 不接受空事件列表。")

        dialogue_text = _format_events_as_dialogue(events)
        summary = self.llm.complete(
            system=_SESSION_COMPRESS_SYSTEM,
            messages=[{"role": "user", "content": dialogue_text}],
        ).strip()
        if not summary:
            raise ValueError("LLM 压缩返回为空,无法写入 session 记忆。")

        return MemoryEntry.new(
            kind=MemoryKind.SESSION,
            period_start=period_start,
            period_end=period_end,
            summary=summary,
            tokens=self.token_counter.count_text(summary),
            source_event_ids=[str(event.get("id")) for event in events if event.get("id")],
        )


# ============================================================== 模块级辅助


def _format_events_as_dialogue(events: Iterable[dict[str, Any]]) -> str:
    """把事件列表渲染成对 LLM 友好的 [角色] 内容 文本块。"""
    lines: list[str] = []
    for event in events:
        role = _role_for_event(event)
        if role is None:
            continue
        content = str(event.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)


def _role_for_event(event: dict[str, Any]) -> str | None:
    """事件类型映射成对话角色;无法识别的事件返回 None 直接忽略。"""
    event_type = event.get("type")
    if event_type == "user_message":
        return "user"
    if event_type in {
        "assistant_response",
        "assistant_natural_response",
        "assistant_question",
    }:
        return "assistant"
    return None
