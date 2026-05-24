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


# session: 服务于"撑过今天",重点是当前话题的事实和未解项。
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

# daily: 服务于长期回忆,日记式总结,直接读全天 raw events。
_DAILY_COMPRESS_SYSTEM = """你是日总结助手。把这一天的全部对话整理成给将来的我看的日记式记忆。

保留:
- 当天发生的事件、用户做的决定
- 用户提到的任务、计划、承诺
- 重要的人物、地点、数字、日期
- 用户状态的变化(情绪、目标、关系)
- 未解决的问题、延后到以后的事项

去掉:
- 客套、寒暄
- 已经被后续推翻的临时讨论
- 重复的确认和复述

直接输出要点。开头一两句"今天总体在做什么",之后分点列出。500~800 字。
"""

# weekly: 把 7 条 daily 合并成"周主线"。
_WEEKLY_COMPRESS_SYSTEM = """你是周总结助手。下面是一周内每天的日总结。把它们合并成一段周总结。

保留:
- 本周的主线(最关注什么、有什么大事)
- 本周做出的决定、形成的习惯、改变的主意
- 跨天延续的任务、还没结束的事
- 被反复提到的人物、关键词

去掉:
- 仅在某一天有意义的细节(具体晚饭、临时琐事)
- 已经在某天彻底解决的小问题

直接输出要点。先一句主线总结,再分点列出。300~500 字。
"""

# monthly: 把若干条 weekly 合并成"月主题"。
_MONTHLY_COMPRESS_SYSTEM = """你是月总结助手。下面是一个月内的若干周总结。把它们合并成一段月总结。

保留:
- 本月的主题或阶段(用户在做什么"大事")
- 本月的关键决定、关系变化、目标调整
- 跨周仍未完成的任务
- 月内出现的重要新人物、新场景

去掉:
- 仅在某一周有意义的细节
- 已经被后续推翻的想法

直接输出要点。先一句主题概括,再分点列出。200~300 字。
"""


class Consolidator:
    """记忆压缩器:把一批源数据交给 LLM,得到一条更高层级的记忆。"""

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
        summary = self._call_llm(_SESSION_COMPRESS_SYSTEM, _format_events_as_dialogue(events))
        return MemoryEntry.new(
            kind=MemoryKind.SESSION,
            period_start=period_start,
            period_end=period_end,
            summary=summary,
            tokens=self.token_counter.count_text(summary),
            source_event_ids=_event_ids(events),
        )

    # -------------------------------------------------------------------- daily

    def compress_to_daily(
        self,
        events: list[dict[str, Any]],
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> MemoryEntry:
        """把一整天的全部原始事件压缩成一条 daily 记忆。"""
        if not events:
            raise ValueError("compress_to_daily 不接受空事件列表。")
        summary = self._call_llm(_DAILY_COMPRESS_SYSTEM, _format_events_as_dialogue(events))
        return MemoryEntry.new(
            kind=MemoryKind.DAILY,
            period_start=period_start,
            period_end=period_end,
            summary=summary,
            tokens=self.token_counter.count_text(summary),
            source_event_ids=_event_ids(events),
        )

    # ------------------------------------------------------------------- weekly

    def compress_to_weekly(
        self,
        daily_memories: list[MemoryEntry],
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> MemoryEntry:
        """把一周内的若干 daily 记忆合并成一条 weekly 记忆。"""
        if not daily_memories:
            raise ValueError("compress_to_weekly 不接受空记忆列表。")
        summary = self._call_llm(
            _WEEKLY_COMPRESS_SYSTEM,
            _format_memories_as_text(daily_memories, kind_label="日"),
        )
        return MemoryEntry.new(
            kind=MemoryKind.WEEKLY,
            period_start=period_start,
            period_end=period_end,
            summary=summary,
            tokens=self.token_counter.count_text(summary),
            source_memory_ids=[entry.id for entry in daily_memories],
        )

    # ------------------------------------------------------------------ monthly

    def compress_to_monthly(
        self,
        weekly_memories: list[MemoryEntry],
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> MemoryEntry:
        """把一个月内的若干 weekly 记忆合并成一条 monthly 记忆。"""
        if not weekly_memories:
            raise ValueError("compress_to_monthly 不接受空记忆列表。")
        summary = self._call_llm(
            _MONTHLY_COMPRESS_SYSTEM,
            _format_memories_as_text(weekly_memories, kind_label="周"),
        )
        return MemoryEntry.new(
            kind=MemoryKind.MONTHLY,
            period_start=period_start,
            period_end=period_end,
            summary=summary,
            tokens=self.token_counter.count_text(summary),
            source_memory_ids=[entry.id for entry in weekly_memories],
        )

    # --------------------------------------------------------------- 内部工具

    def _call_llm(self, system_prompt: str, user_text: str) -> str:
        """统一的 LLM 调用入口,做空响应校验。"""
        summary = self.llm.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        ).strip()
        if not summary:
            raise ValueError("LLM 压缩返回为空,无法写入记忆。")
        return summary


# ============================================================== 模块级辅助


def _event_ids(events: Iterable[dict[str, Any]]) -> list[str]:
    """收集事件 id 列表,跳过缺 id 的异常事件。"""
    return [str(event.get("id")) for event in events if event.get("id")]


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


def _format_memories_as_text(
    memories: list[MemoryEntry],
    *,
    kind_label: str,
) -> str:
    """把多条记忆条目排成给 LLM 看的有序文本。"""
    lines: list[str] = []
    for entry in memories:
        period = entry.period_start.date().isoformat()
        lines.append(f"[{kind_label}总结 {period}]\n{entry.summary.strip()}")
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
