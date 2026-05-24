from __future__ import annotations

import logging
from functools import lru_cache
from typing import Iterable

import tiktoken

logger = logging.getLogger(__name__)

# 没有给出模型名时的默认编码，覆盖大部分 OpenAI 兼容模型。
_FALLBACK_ENCODING = "cl100k_base"

# Chat Completions 协议的每条消息额外开销（role/字段分隔等），按 OpenAI 文档的
# 经验值取 4，足够给所有兼容服务留余量。
_PER_MESSAGE_OVERHEAD = 4


@lru_cache(maxsize=8)
def _get_encoding(model: str | None) -> tiktoken.Encoding:
    """根据模型名拿 tiktoken encoder；模型未知或加载失败时回退到默认编码。"""
    if model:
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            logger.debug("tiktoken 不识别模型 %s，回退到 %s", model, _FALLBACK_ENCODING)
    return tiktoken.get_encoding(_FALLBACK_ENCODING)


class TokenCounter:
    """tiktoken 包装，提供文本和 chat 消息两种 token 统计。"""

    def __init__(self, model: str | None = None) -> None:
        """以模型名初始化 encoder；同一模型只会加载一次。"""
        self._model = model
        self._encoding = _get_encoding(model)

    @property
    def model(self) -> str | None:
        """返回构造时使用的模型名，便于调用方区分缓存。"""
        return self._model

    def count_text(self, text: str) -> int:
        """统计单段文本的 token 数。"""
        if not text:
            return 0
        return len(self._encoding.encode(text))

    def count_messages(self, messages: Iterable[dict[str, str]]) -> int:
        """统计 chat 协议消息列表的 token 数，含每条消息的固定开销。"""
        total = 0
        for message in messages:
            total += _PER_MESSAGE_OVERHEAD
            for value in message.values():
                if isinstance(value, str):
                    total += self.count_text(value)
        return total
