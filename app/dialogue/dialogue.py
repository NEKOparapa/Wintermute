from __future__ import annotations

import logging
from dataclasses import dataclass

from ..event.event import StandardEvent
from ..prompt.prompt import build_messages
from ..storage.storage import GlobalEventStore

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """一次对话处理后的返回结果。"""

    message: str


class DialogueService:
    """对话流程服务，负责处理 L0 用户消息事件。"""

    def __init__(self, store: GlobalEventStore, llm) -> None:
        """注入历史存储和 LLM 客户端，便于测试时替换成假实现。"""
        self.store = store
        self.llm = llm

    def handle_event(self, event: StandardEvent) -> TurnResult:
        """处理一条 L0 用户消息事件，并返回助手回复。"""
        if event.type != "user_message":
            raise ValueError(f"暂不支持的事件类型: {event.type}")

        logger.info("L0 对话事件处理开始 length=%s", len(event.content))

        # 先保存标准事件，后续 prompt 只从事件历史构造，避免重复追加当前输入。
        self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
        )

        prompt = build_messages(self.store.load_events())
        response = self.llm.complete(system=prompt.system, messages=prompt.messages)

        self.store.append_event(
            source="assistant",
            type="assistant_response",
            content=response,
        )
        logger.info("L0 对话事件处理完成 response_length=%s", len(response))
        return TurnResult(message=response)
