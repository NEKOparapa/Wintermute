from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..attention.attention import AttentionLevel, parse_level
from ..event.event import StandardEvent
from ..prompt.prompt import build_l1_messages
from ..storage.storage import GlobalEventStore, MemoryStore
from ..translation.translation import AIResponseType, translate_ai_response

logger = logging.getLogger(__name__)


@dataclass
class ProactiveResult:
    """一次 L1 主动处理后的返回结果。"""

    message: str
    response_type: AIResponseType


class L1ProactiveService:
    """L1 主动唤醒流程服务，独立于 L0 用户对话链路。"""

    def __init__(
        self,
        store: GlobalEventStore,
        memory_store: MemoryStore,
        llm,
    ) -> None:
        self.store = store
        self.memory_store = memory_store
        self.llm = llm

    def handle_event(self, event: StandardEvent) -> ProactiveResult:
        """处理一条 L1 主动事件，并把处理摘要写入当天共享上下文。"""
        level = parse_level(event.attention_level)
        if level is not AttentionLevel.L1:
            raise ValueError(f"L1 主动流程只支持 L1 事件，收到: {event.attention_level}")

        logger.info(
            "L1 主动事件处理开始 source=%s type=%s length=%s",
            event.source,
            event.type,
            len(event.content),
        )

        trigger_event = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
            attention_level=level.value,
        )
        event_date = _event_date(trigger_event)
        prompt = build_l1_messages(event_date, trigger_event)
        response = self.llm.complete(system=prompt.system, messages=prompt.messages)
        translated = translate_ai_response(response.content)

        response_event = self.store.append_event(
            source="assistant",
            type="assistant_l1_response",
            content=translated.raw_response,
            metadata={
                "response_type": translated.response_type.value,
                "trigger_event_id": str(trigger_event.get("id", "")),
            },
            attention_level=level.value,
        )
        self._append_l1_context(trigger_event, response_event, translated.content)

        logger.info(
            "L1 主动事件处理完成 response_type=%s response_length=%s",
            translated.response_type.value,
            len(translated.content),
        )
        return ProactiveResult(
            message=translated.content,
            response_type=translated.response_type,
        )

    def _append_l1_context(
        self,
        trigger_event: dict[str, Any],
        response_event: dict[str, Any],
        response_content: str,
    ) -> None:
        event_date = _event_date(trigger_event)
        label = event_date.isoformat()
        trigger_id = str(trigger_event.get("id", ""))
        response_id = str(response_event.get("id", ""))
        self.memory_store.save_memory(
            kind="l1_context",
            label=label,
            period=_event_period([trigger_event, response_event], fallback_date=event_date),
            content=_context_content(trigger_event, response_content),
            source_event_ids=[item for item in (trigger_id, response_id) if item],
            metadata={
                "event_source": trigger_event.get("source"),
                "event_type": trigger_event.get("type"),
                "trigger_event_id": trigger_id,
                "response_event_id": response_id,
                "status": "handled",
            },
        )


def _context_content(trigger_event: dict[str, Any], response_content: str) -> str:
    source = str(trigger_event.get("source", "")).strip() or "unknown"
    event_type = str(trigger_event.get("type", "")).strip() or "l1_trigger"
    trigger_content = str(trigger_event.get("content", "")).strip()
    response_text = response_content.strip()
    if response_text:
        return f"{source} {event_type}: {trigger_content}；AI 处理结果：{response_text}"
    return f"{source} {event_type}: {trigger_content}"


def _event_period(
    events: list[dict[str, Any]],
    *,
    fallback_date: date,
) -> dict[str, str]:
    label = fallback_date.isoformat()
    if not events:
        start = datetime.combine(fallback_date, datetime.min.time()).astimezone()
        return {
            "start": start.isoformat(timespec="seconds"),
            "end": (start + timedelta(seconds=1)).isoformat(timespec="seconds"),
            "label": label,
        }
    ordered = sorted(events, key=lambda item: str(item.get("timestamp", "")))
    start = _parse_datetime(str(ordered[0].get("timestamp", "")))
    end = _parse_datetime(str(ordered[-1].get("timestamp", ""))) + timedelta(seconds=1)
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": label,
    }


def _event_date(event: dict[str, object]) -> date:
    return _parse_datetime(str(event["timestamp"])).date()


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()
