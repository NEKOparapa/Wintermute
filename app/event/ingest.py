from __future__ import annotations

import logging
from dataclasses import dataclass

from ..memory.consolidator import MemoryConsolidator
from ..storage.storage import GlobalEventStore
from .event import StandardEvent

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """一条背景事件被摄入后的返回结果（不产生对话回复）。"""

    event_id: str
    level: str
    type: str


class EventIngestService:
    """背景事件流程：处理 L2/L3 事件，只落库 + 逐条压缩进事件记忆，不唤起主 AI 对话。"""

    def __init__(self, store: GlobalEventStore, consolidator: MemoryConsolidator) -> None:
        """注入全局事件存储与记忆压缩器。"""
        self.store = store
        self.consolidator = consolidator

    def handle_event(self, event: StandardEvent) -> IngestResult:
        """把背景事件写入事件流（保留原始数据），随后立刻逐条压缩进当天事件记忆。"""
        logger.info(
            "背景事件摄入 level=%s source=%s type=%s length=%s",
            event.attention_level,
            event.source,
            event.type,
            len(event.content),
        )

        # 先落库保留原始事件，daily/weekly/monthly 调度仍会把它卷进长期记忆。
        record = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
            attention_level=event.attention_level,
        )

        # 立刻把这条事件压成一到两行，追加到当天独立的事件记忆文件；失败不影响落库。
        self.consolidator.auto_consolidate_event(record)

        return IngestResult(
            event_id=str(record.get("id", "")),
            level=event.attention_level,
            type=event.type,
        )
