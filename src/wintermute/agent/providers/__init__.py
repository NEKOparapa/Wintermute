"""OpenAI 兼容适配器；导入前需安装 ``openai`` 可选依赖。

``OpenAIModel`` 默认使用异步流式 Responses 适配器。
仅支持 Chat Completions 的服务需显式使用 ``OpenAIChatModel``。
"""

from wintermute.agent.providers.openai import OpenAIChatModel, OpenAIModel, OpenAIResponsesModel

__all__ = ["OpenAIChatModel", "OpenAIModel", "OpenAIResponsesModel"]
