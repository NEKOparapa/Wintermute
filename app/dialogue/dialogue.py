from __future__ import annotations

import logging
from dataclasses import dataclass

from ..event.event import StandardEvent
from ..memory.consolidator import MemoryConsolidator
from ..prompt.prompt import build_messages
from ..storage.storage import GlobalEventStore
from ..translation.translation import AIResponseType, assistant_event_type, translate_ai_response

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """一次对话处理后的返回结果。"""

    message: str
    response_type: AIResponseType


class DialogueService:
    """对话流程服务，负责处理 L0 用户消息事件。"""

    def __init__(
        self,
        store: GlobalEventStore,
        llm,
        consolidator: MemoryConsolidator,
    ) -> None:
        """注入历史存储和 LLM 客户端，便于测试时替换成假实现。"""
        self.store = store
        self.llm = llm
        self.consolidator = consolidator

    def handle_event(self, event: StandardEvent) -> TurnResult:
        """处理一条 L0 用户消息事件，并返回助手回复。"""
        # 1. DialogueService 当前只处理普通用户消息。
        if event.type != "user_message":
            raise ValueError(f"暂不支持的事件类型: {event.type}")

        logger.info("L0 对话事件处理开始 length=%s", len(event.content))

        # 2. 先把用户输入写入 raw event store。
        user_event = self.store.append_event(
            source=event.source,
            type=event.type,
            content=event.content,
            metadata=event.metadata,
        )

        # 3. 交给 MemoryConsolidator 自动判断是否需要压缩 session。
        event_date = self.consolidator.auto_consolidate_session_for_event(user_event)

        # 4. 在对话层构造本轮 LLM prompt。
        prompt = build_messages(event_date)

        # 5. 调用 LLM。
        response = self.llm.complete(system=prompt.system, messages=prompt.messages)

        # 6. 翻译/归一化模型输出。
        translated = translate_ai_response(response)

        # 7. 保存助手回复为 raw event。
        self.store.append_event(
            source="assistant",
            type=assistant_event_type(translated.response_type),
            content=translated.raw_response,
            metadata={"response_type": translated.response_type.value},
        )
        logger.info(
            "L0 对话事件处理完成 response_type=%s response_length=%s",
            translated.response_type.value,
            len(translated.content),
        )

        # 8. 返回本轮对话结果给 HTTP/API 层。
        #    这里不返回 raw_response，是为了隐藏工具调用包装或内部协议，只暴露用户可读内容。
        return TurnResult(
            message=translated.content,
            response_type=translated.response_type,
        )
