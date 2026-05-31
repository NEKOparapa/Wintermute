from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config.config import get_settings
from ..memory.consolidator import MemoryConsolidator
from ..storage.storage import GlobalEventStore
from .event import StandardEvent
from .uploads import upload_local_attachments

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """一条背景事件被摄入后的返回结果（不产生对话回复）。"""

    event_id: str
    level: str
    type: str


class EventIngestService:
    """背景事件流程：处理 L2/L3 事件，只落库 + 逐条压缩进事件记忆，不唤起主 AI 对话。"""

    def __init__(
        self,
        store: GlobalEventStore,
        consolidator: MemoryConsolidator,
        llm: Any,
    ) -> None:
        """注入全局事件存储、记忆压缩器与 LLM（用于上传本地多模态附件）。"""
        self.store = store
        self.consolidator = consolidator
        self.llm = llm

    def handle_event(self, event: StandardEvent) -> IngestResult:
        """落库背景事件（保留原始数据），随后立刻逐条压缩进当天事件记忆。

        若事件携带多模态附件，会先把本地 path 上传成 file_id，再落库与压缩，
        这样压缩阶段才能把图片 / 音频 / 视频 / 文件交给模型描述。
        """
        logger.info(
            "背景事件摄入 level=%s source=%s type=%s length=%s",
            event.attention_level,
            event.source,
            event.type,
            len(event.content),
        )

        # 先把本地附件上传成 file_id（与对话路径共用实现），避免事件流残留临时路径。
        settings = get_settings()
        event = upload_local_attachments(
            event,
            self.llm,
            poll_interval_seconds=settings.file_upload_poll_interval_seconds,
            wait_timeout_seconds=settings.file_upload_timeout_seconds,
        )

        # 落库保留原始事件，daily/weekly/monthly 调度仍会把它卷进长期记忆。
        record = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
            attention_level=event.attention_level,
        )

        # 立刻把这条事件（含多模态内容）压成一到两行，追加到当天事件记忆；失败不影响落库。
        self.consolidator.auto_consolidate_event(record)

        return IngestResult(
            event_id=str(record.get("id", "")),
            level=event.attention_level,
            type=event.type,
        )
