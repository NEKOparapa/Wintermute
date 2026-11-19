"""会话专属的活动明细查询；返回文字，不向模型暴露文件或数据库标识。"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.core.activity import activity_message
from wintermute.events.storage.sqlite import SQLiteStore


class ActivitySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime = Field(description="起始时间（含），ISO 8601，必须含时区")
    end: AwareDatetime = Field(description="结束时间（不含），ISO 8601，必须含时区")
    keyword: str | None = Field(default=None, max_length=200, description="应用名、标题或内容片段")
    terminal: str | None = Field(default=None, max_length=160, description="可选设备名")
    offset: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=8, ge=1, le=20)


def activity_search_tool(store: SQLiteStore, conversation_id: str) -> Tool[ActivitySearch]:
    user_id = store.get_conversation_user(conversation_id)

    async def execute(args: ActivitySearch, _: ToolContext) -> ToolResult:
        rows, more = store.history.search_activity(
            user_id=user_id,
            conversation_id=conversation_id,
            start=args.start,
            end=args.end,
            terminal=args.terminal,
            keyword=args.keyword,
            limit=args.limit,
            offset=args.offset,
        )
        lines: list[str] = []
        # Bound even an unusually large slice description in a tool result.
        for item in rows:
            msg = activity_message(item)
            body = msg.text if len(msg.text) <= 750 else msg.text[:749] + "…"
            lines.append(f"{msg.timestamp} | {msg.origin} | {body}")
        if more:
            lines.append(f"还有匹配记录，以相同条件和 offset={args.offset + len(rows)} 继续。")
        return ToolResult("\n".join(lines) or "这段时间内没有匹配的活动记录。")

    return Tool(
        "search_activity",
        "按时间范围、设备和关键词查本会话的感知与切片明细，结果按时间正序。"
        "时间线摘要省略细节时可查实际时刻、标题及停留秒数；桌面停留按结束时刻检索。",
        ActivitySearch,
        execute,
    )
