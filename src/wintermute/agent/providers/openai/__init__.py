"""OpenAI 兼容协议适配；默认使用流式 Responses。"""

from wintermute.agent.providers.openai.chat import OpenAIChatModel
from wintermute.agent.providers.openai.responses import OpenAIResponsesModel

OpenAIModel = OpenAIResponsesModel

__all__ = ["OpenAIChatModel", "OpenAIModel", "OpenAIResponsesModel"]
