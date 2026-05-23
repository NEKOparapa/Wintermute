from __future__ import annotations

import logging
from dataclasses import dataclass

from .prompt.prompt import build_messages
from .storage.storage import GlobalEventStore

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """一次对话处理后的返回结果。"""

    message: str


class WintermuteService:
    """Wintermute 的核心服务，负责串起存储、上下文构造和 LLM 调用。"""

    def __init__(self, store: GlobalEventStore, llm) -> None:
        """注入历史存储和 LLM 客户端，便于测试时替换成假实现。"""
        self.store = store
        self.llm = llm

    def handle_message(self, message: str) -> TurnResult:
        """处理一条用户输入：先保存用户消息，再带完整历史请求 LLM，最后保存回复。"""
        text = message.strip()
        if not text:
            raise ValueError("message 不能为空。")

        logger.info("用户输入处理开始 length=%s", len(text))
        self.store.append_event(
            source="user",
            type="user_message",
            content=text,
        )

        messages = build_messages(self.store.load_events())
        response = self.llm.complete(messages=messages)
        self.store.append_event(
            source="assistant",
            type="assistant_response",
            content=response,
        )
        logger.info("用户输入处理完成 response_length=%s", len(response))
        return TurnResult(message=response)
