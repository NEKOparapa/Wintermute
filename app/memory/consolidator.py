from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from ..config.config import Settings
from ..llm.llm import OpenAICompatibleLLM
from ..prompt.prompt import build_events_input_message
from ..storage.storage import GlobalEventStore, MemoryStore
from .tokens import count_event_tokens

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = """你负责把历史事件压缩成可供未来对话使用的中文记忆。

要求：
- 只输出摘要正文。
- 保留事实、偏好、待办、决定和长期有用的上下文。
- 省略寒暄、重复内容和无长期价值的过程描述。
- 不编造原始事件中没有的信息。
"""

_EVENT_SUMMARY_SYSTEM = """你负责把单条背景事件（L2/L3，未进入对话）压缩成一到两行中文记忆，供未来对话参考。

要求：
- 只输出摘要正文，最多两行，不加序号或前缀。
- 保留时间、来源和关键事实。
- 不展开过程、不寒暄、不编造原始事件中没有的信息。
"""

_MONTHLY_REFLECTION_SYSTEM = """你负责把月度记忆整理成可供未来对话使用的中文长期记忆。

你可以调用 query_memories 查询历史记忆，用来寻找延续、冲突、重复模式、上下文和潜在趋势。

要求：
- 最多调用 5 次 query_memories。
- 事实、关联、推断分层表达；不要把推断写成事实。
- 只保留有中/高置信度证据支持的关联和推断；低置信内容放入 pending_questions。
- 最终只输出一个 JSON 对象，不要输出 Markdown 或解释文字。
- JSON 必须包含 summary、facts、associations、inferences、pending_questions。
- summary 是可直接注入未来对话的中文月度摘要。
- associations 每项包含 source_id、target_id、relation、reason、confidence。
- relation 只能是 extends、conflicts、repeats、causes、context。
- inferences 每项包含 text、confidence、evidence_ids。
- confidence 只能是 low、medium、high。
- 不编造查询结果和本月周记忆中没有依据的信息。
"""

_QUERY_MEMORIES_TOOL = {
    "type": "function",
    "name": "query_memories",
    "description": "按关键词查询本地长期记忆和会话记忆，返回匹配的记忆摘要。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要查询的关键词、主题或短语。",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 5，最多 8。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": False,
}

_MONTHLY_REFLECTION_VERSION = 1
_MONTHLY_REFLECTION_MODE = "association_query_summary"
_MONTHLY_REFLECTION_MAX_QUERIES = 5
_MEMORY_QUERY_DEFAULT_LIMIT = 5
_MEMORY_QUERY_MAX_LIMIT = 8
_MEMORY_QUERY_KINDS = {"monthly", "weekly", "daily", "session"}
_STABLE_CONFIDENCE = {"medium", "high"}
_ASSOCIATION_RELATIONS = {"extends", "conflicts", "repeats", "causes", "context"}


@dataclass(frozen=True)
class ConsolidationResult:
    """一次压缩任务的结果。"""

    created: bool
    memory: dict[str, Any] | None = None
    reason: str = ""


class MemoryConsolidator:
    """把 raw events 或低层记忆压缩成 session/daily/weekly/monthly 记忆。"""

    def __init__(
        self,
        event_store: GlobalEventStore,
        memory_store: MemoryStore,
        settings: Settings,
    ) -> None:
        self.event_store = event_store
        self.memory_store = memory_store
        self.settings = settings
        self.llm = OpenAICompatibleLLM(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def auto_consolidate_session_for_event(self, user_event: dict[str, object]) -> date:
        """根据用户事件日期自动判断并压缩 session，失败时只记录日志。"""
        current_date = _event_date(user_event)
        try:
            self.maybe_consolidate_session(today=current_date)
        except Exception:
            logger.exception("session 记忆压缩失败，继续使用当前 raw events")
        return current_date

    def auto_consolidate_event(self, event: dict[str, object]) -> date:
        """L2/L3 背景事件落库后逐条压缩进当天事件记忆，失败时只记录日志。"""
        current_date = _event_date(event)
        try:
            self.consolidate_event(event)
        except Exception:
            logger.exception("事件记忆压缩失败，原始事件已保留")
        return current_date

    def consolidate_event(self, event: dict[str, Any]) -> ConsolidationResult:
        """把单条背景事件压缩成一到两行，追加到当天的事件记忆文件。"""
        event_id = str(event.get("id", ""))
        if not event_id:
            return ConsolidationResult(False, reason="missing_event_id")

        event_date = _event_date(event)
        label = event_date.isoformat()
        if event_id in self.memory_store.source_event_ids_for_event(label):
            return ConsolidationResult(False, reason="already_compressed")

        content = self._summarize_single_event(event)
        memory = self.memory_store.save_memory(
            kind="event",
            label=label,
            period=_event_period([event], fallback_date=event_date, label=label),
            content=content,
            source_event_ids=[event_id],
            metadata={
                "attention_level": event.get("attention_level"),
                "event_source": event.get("source"),
                "event_type": event.get("type"),
            },
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def maybe_consolidate_session(self, *, today: date | None = None) -> ConsolidationResult:
        """当今天 raw events 超过阈值时，压缩最近 N 轮之前的未压缩事件。"""
        current_date = today or datetime.now().astimezone().date()
        events = self.event_store.load_events_for_date(current_date)
        token_count = count_event_tokens(events)
        if token_count <= self.settings.session_token_threshold:
            return ConsolidationResult(False, reason="below_threshold")

        candidates = self._session_candidates(events)
        compressed_ids = self.memory_store.source_event_ids_for_session(current_date.isoformat())
        candidates = [
            event
            for event in candidates
            if str(event.get("id", "")) and str(event.get("id", "")) not in compressed_ids
        ]
        if not candidates:
            return ConsolidationResult(False, reason="no_uncompressed_events")

        label = current_date.isoformat()
        content = self._summarize_events("session", label, candidates)
        memory = self.memory_store.save_memory(
            kind="session",
            label=label,
            period=_event_period(candidates, fallback_date=current_date, label=label),
            content=content,
            source_event_ids=[str(event["id"]) for event in candidates if event.get("id")],
            metadata={"token_count": token_count},
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def consolidate_daily(self, target_date: date) -> ConsolidationResult:
        """压缩指定自然日的全部 raw events；目标文件存在时跳过。"""
        label = target_date.isoformat()
        if self.memory_store.memory_exists("daily", label):
            return ConsolidationResult(False, reason="exists")

        events = self.event_store.load_events_for_date(target_date)
        if events:
            content = self._summarize_events("daily", label, events)
        else:
            content = "当天没有记录事件。"
        memory = self.memory_store.save_memory(
            kind="daily",
            label=label,
            period=_day_period(target_date),
            content=content,
            source_event_ids=[str(event["id"]) for event in events if event.get("id")],
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def consolidate_weekly(self, week_start: date) -> ConsolidationResult:
        """压缩指定 ISO 周；缺任一天 daily 时跳过。"""
        week_start = week_start - timedelta(days=week_start.weekday())
        label = _week_label(week_start)
        if self.memory_store.memory_exists("weekly", label):
            return ConsolidationResult(False, reason="exists")

        daily_memories: list[dict[str, Any]] = []
        for offset in range(7):
            daily_label = (week_start + timedelta(days=offset)).isoformat()
            memory = self.memory_store.load_memory("daily", daily_label)
            if memory is None:
                return ConsolidationResult(False, reason=f"missing_daily:{daily_label}")
            daily_memories.append(memory)

        content = self._summarize_memories("weekly", label, daily_memories)
        memory = self.memory_store.save_memory(
            kind="weekly",
            label=label,
            period=_week_period(week_start),
            content=content,
            source_memory_ids=[str(item["id"]) for item in daily_memories if item.get("id")],
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def consolidate_monthly(self, month_start: date) -> ConsolidationResult:
        """压缩指定自然月内所有已存在且 period 相交的 weekly 记忆。"""
        month_start = month_start.replace(day=1)
        label = month_start.strftime("%Y-%m")
        if self.memory_store.memory_exists("monthly", label):
            return ConsolidationResult(False, reason="exists")

        period = _month_period(month_start)
        # 一次性读取全部记忆：既用于找本月 weekly 主证据，也用于构造可查询的历史语料。
        all_memories = self.memory_store.load_all_memories()
        # 月度总结的主输入只取与目标自然月相交的 weekly 记忆，保证 summary 以本月事实为核心。
        weekly_memories = [
            memory
            for memory in all_memories
            if memory.get("kind") == "weekly" and _memory_overlaps(memory, period)
        ]
        weekly_memories.sort(key=lambda item: str(item.get("period", {}).get("start", "")))
        if weekly_memories:
            # 有本月周记忆时进入“慢思考”：模型可以主动查询旧记忆，再输出新月度记忆。
            content, metadata = self._reflect_monthly(
                label,
                period,
                weekly_memories,
                all_memories,
            )
        else:
            # 空月不触发 LLM 反思，保持确定、便宜、可预测的占位记忆。
            content = "上月没有可用周记忆。"
            metadata = None
        memory = self.memory_store.save_memory(
            kind="monthly",
            label=label,
            period=period,
            content=content,
            source_memory_ids=[str(item["id"]) for item in weekly_memories if item.get("id")],
            metadata=metadata,
        )
        return ConsolidationResult(memory is not None, memory=memory, reason="created")

    def _session_candidates(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        dialogue_events = [event for event in events if _is_dialogue_event(event)]
        user_indexes = [
            index for index, event in enumerate(dialogue_events) if event.get("type") == "user_message"
        ]
        recent_turns = self.settings.prompt_recent_turns
        if len(user_indexes) <= recent_turns:
            return []
        return dialogue_events[: user_indexes[-recent_turns]]

    def _summarize_events(
        self,
        kind: str,
        label: str,
        events: list[dict[str, Any]],
    ) -> str:
        return self.llm.complete(
            system=_SUMMARY_SYSTEM,
            messages=[
                build_events_input_message(
                    f"请压缩 {kind} {label} 的原始事件：\n\n{_format_events(events)}",
                    events,
                )
            ],
        ).content

    def _summarize_single_event(self, event: dict[str, Any]) -> str:
        return self.llm.complete(
            system=_EVENT_SUMMARY_SYSTEM,
            messages=[
                build_events_input_message(
                    f"请把这条背景事件压缩成一到两行：\n\n{_format_events([event])}",
                    [event],
                )
            ],
        ).content

    def _summarize_memories(
        self,
        kind: str,
        label: str,
        memories: list[dict[str, Any]],
    ) -> str:
        return self.llm.complete(
            system=_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"请压缩 {kind} {label} 的下层记忆：\n\n{_format_memories(memories)}",
                }
            ],
        ).content

    def _reflect_monthly(
        self,
        label: str,
        period: dict[str, str],
        weekly_memories: list[dict[str, Any]],
        all_memories: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """月度慢思考：允许模型查询旧记忆，再输出摘要和结构化反思元数据。"""
        # 查询语料是只读的本地记忆切片；模型只能通过 query_memories 读取这里面的内容。
        corpus = _monthly_reflection_corpus(all_memories, period)
        # queries 记录模型实际查了什么，最终随 metadata 持久化，方便以后追踪推断来源。
        queries: list[dict[str, Any]] = []
        try:
            text = self._complete_monthly_reflection(
                label,
                period,
                weekly_memories,
                corpus,
                queries,
            )
            # 模型最终必须给 JSON；summary 进入 content，其余结构化反思进入 metadata。
            payload = _parse_json_object(text)
            return _monthly_reflection_from_payload(payload, queries)
        except ValueError as exc:
            # 如果模型没给出合法 JSON，不丢月度记忆：回退到旧的直接摘要流程。
            logger.warning("monthly 反思结果无法解析，回退普通摘要 label=%s error=%s", label, exc)
            content = self._summarize_memories("monthly", label, weekly_memories)
            return content, {
                "monthly_reflection": _monthly_reflection_metadata(
                    queries=queries,
                    facts=[],
                    associations=[],
                    inferences=[],
                    pending_questions=[],
                    error=str(exc),
                )
            }

    def _complete_monthly_reflection(
        self,
        label: str,
        period: dict[str, str],
        weekly_memories: list[dict[str, Any]],
        corpus: list[dict[str, Any]],
        queries: list[dict[str, Any]],
    ) -> str:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": _monthly_reflection_prompt(label, period, weekly_memories),
            }
        ]
        query_calls = 0
        # 允许最多 5 次真实查询；多一轮是为了让模型在拿到最后一次查询结果后输出最终 JSON。
        for _ in range(_MONTHLY_REFLECTION_MAX_QUERIES + 1):
            response = self.llm.complete(
                system=_MONTHLY_REFLECTION_SYSTEM,
                messages=messages,
                tools=[_QUERY_MEMORIES_TOOL],
            )
            if not response.tool_calls:
                return response.content

            for call in response.tool_calls:
                call_id = str(call.id or f"query_memories_{len(messages)}")
                # 把模型的工具请求和工具结果都放回 messages，维持 Responses function_call 协议。
                messages.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
                if call.name != "query_memories":
                    output = json.dumps(
                        {"error": f"unknown_tool:{call.name}"},
                        ensure_ascii=False,
                    )
                elif query_calls >= _MONTHLY_REFLECTION_MAX_QUERIES:
                    output = json.dumps(
                        {"error": "query_budget_exceeded"},
                        ensure_ascii=False,
                    )
                else:
                    query_calls += 1
                    # query_memories 是月度反思内部的只读工具，不走主对话的工具注册表。
                    output = self._query_monthly_memories(call.arguments, corpus, queries)
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    }
                )
        raise ValueError("no_final_monthly_reflection")

    def _query_monthly_memories(
        self,
        arguments: str,
        corpus: list[dict[str, Any]],
        queries: list[dict[str, Any]],
    ) -> str:
        try:
            payload = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps(
                {"error": "invalid_arguments_json", "raw": arguments},
                ensure_ascii=False,
            )
        if not isinstance(payload, dict):
            return json.dumps({"error": "arguments_not_object"}, ensure_ascii=False)

        query = str(payload.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "missing_query"}, ensure_ascii=False)

        limit = _coerce_query_limit(payload.get("limit"))
        results = _query_memory_corpus(corpus, query, limit)
        result_ids = [str(item["id"]) for item in results if item.get("id")]
        # metadata 只记录查询词、limit 和命中的记忆 id；完整查询结果只回喂给本轮模型。
        queries.append({"query": query, "limit": limit, "result_ids": result_ids})
        return json.dumps(
            {
                "query": query,
                "limit": limit,
                "results": results,
            },
            ensure_ascii=False,
        )


def previous_day(now: datetime | None = None) -> date:
    current = now or datetime.now().astimezone()
    return current.date() - timedelta(days=1)


def previous_week_start(now: datetime | None = None) -> date:
    current = now or datetime.now().astimezone()
    this_week_start = current.date() - timedelta(days=current.date().weekday())
    return this_week_start - timedelta(days=7)


def previous_month_start(now: datetime | None = None) -> date:
    current = now or datetime.now().astimezone()
    first_this_month = current.date().replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    return last_prev_month.replace(day=1)


def _format_events(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events:
        lines.append(
            f"- {event.get('timestamp')} {event.get('source')} {event.get('type')}: "
            f"{event.get('content', '')}"
        )
    return "\n".join(lines)


def _format_memories(memories: list[dict[str, Any]]) -> str:
    lines = []
    for memory in memories:
        period = memory.get("period", {})
        label = period.get("label", "") if isinstance(period, dict) else ""
        lines.append(f"- {memory.get('kind')} {label}: {memory.get('content', '')}")
    return "\n".join(lines)


def _monthly_reflection_prompt(
    label: str,
    period: dict[str, str],
    weekly_memories: list[dict[str, Any]],
) -> str:
    return "\n\n".join(
        [
            f"请对 monthly {label} 做月度记忆反思。",
            f"目标周期：{period.get('start')} 至 {period.get('end')}",
            "# 本月周记忆\n" + _format_reflection_memories(weekly_memories),
            "请先按需要查询历史记忆，再输出最终 JSON。",
        ]
    )


def _format_reflection_memories(memories: list[dict[str, Any]]) -> str:
    lines = []
    for memory in memories:
        period = memory.get("period", {})
        label = period.get("label", "") if isinstance(period, dict) else ""
        lines.append(
            f"- id={memory.get('id', '')} kind={memory.get('kind')} label={label}: "
            f"{memory.get('content', '')}"
        )
    return "\n".join(lines)


def _monthly_reflection_corpus(
    memories: list[dict[str, Any]],
    target_period: dict[str, str],
) -> list[dict[str, Any]]:
    target_end = _parse_datetime(target_period["end"])
    corpus: list[dict[str, Any]] = []
    for memory in memories:
        kind = str(memory.get("kind", ""))
        # 只允许查询长期/会话层记忆；event 是背景事件碎片，噪音较大，排除在月度反思查询外。
        if kind not in _MEMORY_QUERY_KINDS:
            continue
        memory_id = str(memory.get("id", "")).strip()
        content = str(memory.get("content", "")).strip()
        period = memory.get("period")
        if not memory_id or not content or not isinstance(period, dict):
            continue
        try:
            start = _parse_datetime(str(period["start"]))
            end = _parse_datetime(str(period["end"]))
        except (KeyError, ValueError):
            continue
        # 不让月度总结看到目标月结束之后的未来记忆；当前版本允许补查本月 daily/session 细节。
        if start >= target_end:
            continue
        corpus.append(
            {
                "id": memory_id,
                "kind": kind,
                "label": str(period.get("label", "")),
                "period": {
                    "start": start.isoformat(timespec="seconds"),
                    "end": end.isoformat(timespec="seconds"),
                },
                "content": content,
            }
        )
    return corpus


def _query_memory_corpus(
    corpus: list[dict[str, Any]],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    query_text = query.strip().lower()
    if not query_text and not terms:
        return []

    ranked: list[tuple[int, float, int, dict[str, Any]]] = []
    for index, memory in enumerate(corpus):
        # v1 不建向量索引，只在 kind/label/content 上做轻量关键词匹配。
        haystack = " ".join(
            [
                str(memory.get("kind", "")),
                str(memory.get("label", "")),
                str(memory.get("content", "")),
            ]
        ).lower()
        score = 0
        if query_text and query_text in haystack:
            score += max(1, len(query_text)) * 4
        for term in terms:
            if term in haystack:
                score += max(1, len(term))
        if score <= 0:
            continue
        start = _parse_datetime(str(memory.get("period", {}).get("start", ""))).timestamp()
        ranked.append((score, start, index, memory))

    # 分数越高越靠前；同分时优先较新的记忆，最后用原始顺序保证排序稳定。
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [dict(item[3]) for item in ranked[:limit]]


def _query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.split(r"[\s,，。；;:：、]+", query.strip().lower())
        if term
    ]


def _coerce_query_limit(value: object) -> int:
    try:
        limit = int(value) if value is not None else _MEMORY_QUERY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _MEMORY_QUERY_DEFAULT_LIMIT
    return max(1, min(limit, _MEMORY_QUERY_MAX_LIMIT))


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_json_fence(text.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("invalid_json") from exc
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as nested_exc:
            raise ValueError("invalid_json") from nested_exc
    if not isinstance(payload, dict):
        raise ValueError("json_not_object")
    return payload


def _strip_json_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _monthly_reflection_from_payload(
    payload: dict[str, Any],
    queries: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    summary = _clean_text(payload.get("summary"))
    if not summary:
        raise ValueError("missing_summary")

    associations, association_questions = _clean_associations(payload.get("associations"))
    inferences, inference_questions = _clean_inferences(payload.get("inferences"))
    pending_questions = _clean_text_list(payload.get("pending_questions"))
    pending_questions.extend(association_questions)
    pending_questions.extend(inference_questions)

    return summary, {
        "monthly_reflection": _monthly_reflection_metadata(
            queries=queries,
            facts=_clean_text_list(payload.get("facts")),
            associations=associations,
            inferences=inferences,
            pending_questions=pending_questions,
        )
    }


def _monthly_reflection_metadata(
    *,
    queries: list[dict[str, Any]],
    facts: list[str],
    associations: list[dict[str, Any]],
    inferences: list[dict[str, Any]],
    pending_questions: list[str],
    error: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "version": _MONTHLY_REFLECTION_VERSION,
        "mode": _MONTHLY_REFLECTION_MODE,
        "queries": [dict(item) for item in queries],
        "facts": facts,
        "associations": associations,
        "inferences": inferences,
        "pending_questions": pending_questions,
    }
    if error:
        metadata["error"] = error
    return metadata


def _clean_associations(value: object) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], []
    associations: list[dict[str, Any]] = []
    pending_questions: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        confidence = _normalize_confidence(item.get("confidence"))
        if confidence not in _STABLE_CONFIDENCE:
            question = _low_confidence_question(item.get("reason"))
            if question:
                pending_questions.append(question)
            continue
        source_id = _clean_text(item.get("source_id"))
        target_id = _clean_text(item.get("target_id"))
        reason = _clean_text(item.get("reason"))
        if not source_id or not target_id or not reason:
            continue
        relation = _clean_text(item.get("relation")).lower()
        if relation not in _ASSOCIATION_RELATIONS:
            relation = "context"
        associations.append(
            {
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "reason": reason,
                "confidence": confidence,
            }
        )
    return associations, pending_questions


def _clean_inferences(value: object) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], []
    inferences: list[dict[str, Any]] = []
    pending_questions: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"))
        if not text:
            continue
        confidence = _normalize_confidence(item.get("confidence"))
        if confidence not in _STABLE_CONFIDENCE:
            question = _low_confidence_question(text)
            if question:
                pending_questions.append(question)
            continue
        inferences.append(
            {
                "text": text,
                "confidence": confidence,
                "evidence_ids": _clean_text_list(item.get("evidence_ids")),
            }
        )
    return inferences, pending_questions


def _clean_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text:
            cleaned.append(text)
    return cleaned


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_confidence(value: object) -> str:
    text = _clean_text(value).lower()
    mapping = {
        "high": "high",
        "高": "high",
        "高置信": "high",
        "medium": "medium",
        "med": "medium",
        "中": "medium",
        "中等": "medium",
        "中置信": "medium",
        "low": "low",
        "低": "low",
        "低置信": "low",
    }
    return mapping.get(text, text)


def _low_confidence_question(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return f"待验证：{text}"


def _event_period(events: list[dict[str, Any]], *, fallback_date: date, label: str) -> dict[str, str]:
    if not events:
        return _day_period(fallback_date)
    start = _parse_datetime(str(events[0].get("timestamp", "")))
    end = _parse_datetime(str(events[-1].get("timestamp", ""))) + timedelta(seconds=1)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": label,
    }


def _day_period(target_date: date) -> dict[str, str]:
    start = datetime.combine(target_date, time.min).astimezone()
    end = start + timedelta(days=1)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": target_date.isoformat(),
    }


def _week_period(week_start: date) -> dict[str, str]:
    start = datetime.combine(week_start, time.min).astimezone()
    end = start + timedelta(days=7)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": _week_label(week_start),
    }


def _month_period(month_start: date) -> dict[str, str]:
    start = datetime.combine(month_start, time.min).astimezone()
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    end = datetime.combine(next_month, time.min).astimezone()
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": month_start.strftime("%Y-%m"),
    }


def _week_label(week_start: date) -> str:
    iso_year, iso_week, _ = week_start.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _memory_overlaps(memory: dict[str, Any], period: dict[str, str]) -> bool:
    memory_period = memory.get("period")
    if not isinstance(memory_period, dict):
        return False
    try:
        left_start = _parse_datetime(str(memory_period["start"]))
        left_end = _parse_datetime(str(memory_period["end"]))
        right_start = _parse_datetime(period["start"])
        right_end = _parse_datetime(period["end"])
    except (KeyError, ValueError):
        return False
    return left_start < right_end and right_start < left_end


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def _event_date(event: dict[str, object]) -> date:
    return _parse_datetime(str(event["timestamp"])).date()


def _is_dialogue_event(event: dict[str, Any]) -> bool:
    if str(event.get("attention_level", "")).upper() == "L1":
        return False
    return event.get("type") in {
        "user_message",
        "assistant_response",
        "assistant_natural_response",
        "assistant_question",
        "assistant_tool_call",
        "tool_result",
    }
