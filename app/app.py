from __future__ import annotations

import logging
from dataclasses import dataclass

from .llm.llm import LLM, LLMError
from .models.models import Event
from .storage.storage import FileStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。

只根据当前用户输入回复。
"""


@dataclass
class TurnResult:
    response: str


class WintermuteApp:
    def __init__(
        self,
        store: FileStore,
        llm: LLM,
    ) -> None:
        self.store = store
        self.llm = llm

    def bootstrap(self) -> None:
        self.store.ensure()
        logger.info("存储初始化完成 data_dir=%s", self.store.data_dir)

    def handle_user_input(self, text: str) -> TurnResult:
        logger.info("用户输入处理开始 length=%s", len(text))
        self.store.append_event(
            Event.create(
                level="L3",
                source="user",
                type="user_message",
                content=text,
            )
        )
        user_prompt = f"# 当前用户输入\n{text}"
        try:
            response = self.llm.complete(system=SYSTEM_PROMPT, user=user_prompt)
        except LLMError as exc:
            logger.error("LLM 请求失败: %s", exc)
            response = str(exc)

        self.store.append_event(
            Event.create(
                level="L1",
                source="system",
                type="assistant_response",
                content=response,
            )
        )
        logger.info(
            "用户输入处理完成 response_length=%s",
            len(response),
        )
        return TurnResult(response=response)
