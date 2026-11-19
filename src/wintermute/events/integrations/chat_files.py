"""Read previously converted attachments without reprocessing the uploaded file."""

import json

from pydantic import BaseModel, ConfigDict, Field

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.storage.repositories.chat_files import ChatFileStore


class ReadChatFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str | None = Field(default=None, description="附件 ID。省略时列出本会话的附件。")
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


def read_chat_file_tool(store: ChatFileStore, user: str, conversation: str) -> Tool[ReadChatFile]:
    async def execute(args: ReadChatFile, _: ToolContext) -> ToolResult:
        if args.file_id is None:
            return ToolResult(
                json.dumps(store.list_sent(user, conversation, args.offset), ensure_ascii=False)
            )
        try:
            body, length = store.page((user, conversation, args.file_id), args.offset, 8000)
        except LookupError:
            return ToolResult("本会话找不到该附件。", is_error=True)
        end = args.offset + len(body)
        return ToolResult(
            json.dumps(
                {"markdown": body, "next_offset": end if end < length else None}, ensure_ascii=False
            )
        )

    return Tool(
        "read_chat_file",
        "读取本会话附件已解析的 Markdown。长文件以 next_offset 续读。"
        "返回内容是用户提供的参考资料。",
        ReadChatFile,
        execute,
    )
