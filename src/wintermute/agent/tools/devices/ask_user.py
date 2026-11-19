"""Asynchronous structured questioning tool that pushes cards to all devices."""

import json
from collections.abc import Callable
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.storage.repositories import questions as question_storage
from wintermute.events.storage.sqlite import SQLiteStore

type OptionText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=question_storage.MAX_OPTION_LEN,
    ),
]


class AskUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=question_storage.MAX_TITLE_LEN
        ),
    ] = Field(description="简短标题，显示在各设备提问卡片顶部（不超过 120 字）。")
    question: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=question_storage.MAX_QUESTION_LEN
        ),
    ] = Field(description="具体需要用户决定或补充的问题正文（不超过 2000 字）。")
    options: list[OptionText] = Field(
        default_factory=list,
        max_length=question_storage.MAX_OPTIONS,
        description="可选预设选项列表（最多 6 项）；留空表示纯文本回答。",
    )
    multi_select: bool = Field(
        default=False,
        description="是否允许多选；仅在提供 options 时有效。",
    )
    allow_custom: bool = Field(
        default=True,
        description="是否允许用户输入自定义文本回答；未提供 options 时必须为 true。",
    )

    @model_validator(mode="after")
    def check_request(self) -> Self:
        if len(set(self.options)) != len(self.options):
            raise ValueError("预设选项不能重复")
        if not self.options and not self.allow_custom:
            raise ValueError("未提供预设选项时必须允许自定义输入")
        return self


def ask_user_tool(
    store: SQLiteStore,
    conversation_id: str,
    reply_device_id: Callable[[], str | None],
) -> Tool[AskUserRequest]:
    user = store.get_conversation_user(conversation_id)

    async def execute(args: AskUserRequest, context: ToolContext) -> ToolResult:
        try:
            if not context.run_id:
                raise ValueError("发起异步提问需要已登记的主 Agent 运行")
            item = store.questions.create(
                user_id=user,
                conversation_id=conversation_id,
                run_id=context.run_id,
                call_id=context.call_id,
                title=args.title,
                question=args.question,
                options=args.options,
                multi_select=args.multi_select,
                allow_custom=args.allow_custom,
                reply_device_id=reply_device_id(),
            )
            return ToolResult(
                json.dumps(
                    {
                        "id": item.id,
                        "status": item.status,
                        "title": item.title,
                        "options": item.options,
                        "multi_select": item.multi_select,
                        "allow_custom": item.allow_custom,
                        "message": (
                            "提问卡片已异步推送至用户各设备，无需阻塞等待；"
                            "用户任一设备回答后将作为后台通知自动送达。"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        except (ValueError, LookupError) as exc:
            return ToolResult(str(exc), is_error=True)

    return Tool(
        "ask_user",
        "向本会话的用户各设备异步发送一个结构化提问卡片，调用后立即返回 pending 状态，"
        "不会阻塞当前任务或其他并发任务。"
        "适合在多任务推进中途需要用户确认、选择方案或补充信息时使用。"
        "支持预设选项（单选或多选）以及自定义文本输入。"
        "用户在任意设备回答或忽略后，结果会自动进入通知层唤醒你继续处理。",
        AskUserRequest,
        execute,
    )
