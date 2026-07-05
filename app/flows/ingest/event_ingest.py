from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ...memory.consolidator import EventMemoryConsolidator
from ...infrastructure.storage.storage import GlobalEventStore

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """一条背景事件被摄入后的返回结果（不产生对话回复）。"""

    event_id: str
    level: str
    type: str


class _BaseEventIngestService:
    """单层级背景事件摄入服务：落 raw event，再压缩进该层级自己的事件记忆。"""

    level: str

    def __init__(
        self,
        store: GlobalEventStore,
        consolidator: EventMemoryConsolidator,
    ) -> None:
        self.store = store
        self.consolidator = consolidator

    def handle_event(self, event: dict[str, Any]) -> IngestResult:
        """把背景事件写入本层事件流，随后压缩进本层事件记忆。"""
        content = str(event.get("content") or "")
        event_type = str(event.get("type") or "")
        logger.info(
            "%s 背景事件摄入 source=%s type=%s length=%s",
            self.level,
            event.get("source"),
            event_type,
            len(content),
        )

        record = self.store.append_event(
            source=str(event.get("source") or ""),
            type=event_type,
            content=content,
            metadata=event.get("metadata"),
            attention_level=self.level,
        )
        self.consolidator.auto_consolidate_event(record)

        return IngestResult(
            event_id=str(record.get("id", "")),
            level=self.level,
            type=event_type,
        )


class L2EventIngestService(_BaseEventIngestService):
    """L2 背景事件服务。"""

    level = "L2"


class L3EventIngestService(_BaseEventIngestService):
    """L3 背景事件服务。"""

    level = "L3"
