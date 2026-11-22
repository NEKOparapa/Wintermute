"""将一次 Runner 执行的消息、工具边界和回复目标绑定到持久化任务。"""

import asyncio
from functools import partial

from wintermute.agent import AssistantMessage, Message, RunControl, ToolCall, UserMessage
from wintermute.agent.context import ContextCheckpoint
from wintermute.events.core.types import EventRecord
from wintermute.events.storage.sqlite import SQLiteStore


class SessionRun:
    def __init__(
        self,
        run_id: str,
        trigger: EventRecord,
        store: SQLiteStore,
        owner_id: str,
        references: tuple[UserMessage, ...] = (),
    ) -> None:
        if trigger.conversation_id is None:
            raise ValueError("Session runs require a conversation")
        self.run_id = run_id
        self.store = store
        self.owner_id = owner_id
        self.references = references
        self.reply_device_id = trigger.reply_device_id
        self._needs_routing = self.store.sessions.job_input_applied(run_id)
        self._retried_timeline = False
        # 跨端排队的输入已在上次运行入史；新触发则随首个时间线批次一起接纳。
        self._requests = [trigger] if self._needs_routing else []
        self.control = RunControl(
            run_id=run_id,
            wake=asyncio.Event(),
            has_inputs=partial(self.store.sessions.has_pending_inputs, run_id),
            adopt=self._adopt,
            finish=self._finish,
            declare_tools=self._declare_tools,
            start_tool=self._start_tool,
            end_tool=partial(self.store.sessions.finish_tool, run_id, owner_id=owner_id),
            send_replies=partial(self.store.sessions.finish_tool, run_id, owner_id=owner_id),
            end_session=partial(self.store.sessions.end_session, run_id, owner_id=owner_id),
            reply_task=self._reply_task,
            model_read_count=store.sessions.load_model_read_count(
                trigger.conversation_id, trigger.user_id
            ),
            record_model_read=self._record_model_read,
        )

    def _record_model_read(
        self, count: int, transcript: tuple[Message, ...], checkpoint: ContextCheckpoint | None
    ) -> None:
        self.store.sessions.record_model_read(
            self.run_id, count, checkpoint, messages=transcript, owner_id=self.owner_id
        )

    def _adopt(
        self, transcript: tuple[Message, ...]
    ) -> tuple[tuple[UserMessage, ...], tuple[UserMessage, ...]]:
        inputs = self.store.sessions.absorb_session_inputs(
            self.run_id, messages=transcript, owner_id=self.owner_id
        )
        messages: list[UserMessage] = []
        for item in inputs:
            if isinstance(item, UserMessage):
                messages.append(item)
                continue
            messages.append(self.store.sessions.input_message(item))
            if item.reply_device_id == self.reply_device_id:
                self._requests.append(item)
            else:
                self._needs_routing = True
        return tuple(messages), self.references

    def _reply_task(self) -> UserMessage | None:
        if not self._needs_routing:
            return None
        # 即使历史已被摘要，仍能定位原话和带来源的通知结果。
        requests = "\n\n".join(
            self.store.sessions.input_message(event).model_text() for event in self._requests
        )
        return UserMessage(
            "[本轮回复任务]\n请向下列设备答复其待答输入。其他设备对当前动作的更正立即生效，"
            "其他设备另外委派的操作留给其专属回复。复用历史中已有的工具结果，避免重复操作。"
            "后台通知按来源数据处理。\n"
            f"回复设备：{self.reply_device_id or '会话'}\n待答输入：\n{requests}\n[/本轮回复任务]"
        )

    def _declare_tools(self, prefix: tuple[Message, ...], batch: AssistantMessage) -> None:
        self.store.sessions.register_tool_batch(
            self.run_id, messages=(*prefix, batch), owner_id=self.owner_id
        )

    def _start_tool(self, call: ToolCall) -> bool:
        return self.store.sessions.try_start_tool(self.run_id, call.id, owner_id=self.owner_id)

    def _finish(self, messages: tuple[Message, ...], response: AssistantMessage) -> bool:
        result = self.store.sessions.finish_reply_attempt(
            self.run_id,
            messages=messages,
            response=response.text,
            owner_id=self.owner_id,
            check_activity=not self._retried_timeline,
        )
        if result == "timeline":
            # 单独到达的观测最多引发一次补读，持续切换窗口时也能交付本轮回复。
            self._retried_timeline = True
        return result == "completed"
