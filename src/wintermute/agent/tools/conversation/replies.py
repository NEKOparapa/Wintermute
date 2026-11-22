"""主会话的即时发言与静默结束工具。"""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from wintermute.agent import Tool, ToolContext, ToolResult


class QuickReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = Field(
        min_length=1, description="按发送顺序排列的发言，每段单独显示为一条聊天消息。"
    )


class EndSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _quick_reply(request: QuickReplyRequest, context: ToolContext) -> ToolResult:
    return ToolResult(f"已提交 {len(request.messages)} 条发言。", replies=tuple(request.messages))


async def _end_session(request: EndSessionRequest, context: ToolContext) -> ToolResult:
    return ToolResult("终止成功", end_session=True)


def conversation_reply_tools() -> tuple[Tool[Any], ...]:
    return (
        Tool(
            "quick_reply",
            "立即向当前回复目标发送一条或多条聊天消息，随后继续工作。"
            "可用于在耗时操作前告知用户计划或将使用的工具。最终答复无需重复已发送的内容。",
            QuickReplyRequest,
            _quick_reply,
        ),
        Tool(
            "end_session",
            "静默结束当前这轮运行。收到后台通知后，可直接结束，也可先调查再决定。"
            "有待答的用户问题时先答复。成功后跳过剩余工具，停止请求模型，保留会话历史。",
            EndSessionRequest,
            _end_session,
        ),
    )
