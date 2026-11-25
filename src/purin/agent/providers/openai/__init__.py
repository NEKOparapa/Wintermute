"""OpenAI 兼容协议适配；默认使用流式 Responses。"""

from purin.agent.providers.openai.chat import OpenAIChatModel
from purin.agent.providers.openai.responses import OpenAIResponsesModel

OpenAIModel = OpenAIResponsesModel

__all__ = ["OpenAIChatModel", "OpenAIModel", "OpenAIResponsesModel"]
