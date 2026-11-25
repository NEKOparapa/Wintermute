"""执行不含工具调用的文本模型请求，并关闭流与验证最终响应。"""

import asyncio

from purin.agent.core.errors import ModelProtocolError
from purin.agent.core.types import AgentContext, AssistantMessage, Model, TextDelta


async def complete_text(model: Model, context: AgentContext, timeout_seconds: float) -> str:
    async with asyncio.timeout(timeout_seconds):
        stream = model.stream(context)
        final: AssistantMessage | None = None
        try:
            async for event in stream:
                if final is not None:
                    raise ModelProtocolError("Text model emitted events after final text")
                if isinstance(event, AssistantMessage):
                    final = event
                elif not isinstance(event, TextDelta):
                    raise ModelProtocolError("Text model emitted an unsupported event")
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
    if final is None or final.tool_calls or final.finish_reason != "stop":
        raise ModelProtocolError("Text model must return complete text without tools")
    if not final.text.strip():
        raise ModelProtocolError("Text model returned empty text")
    return final.text.strip()
