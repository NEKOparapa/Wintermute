"""从持久会话历史恢复主 Agent 的输入。"""

from dataclasses import dataclass

from wintermute.agent import Message, UserMessage
from wintermute.events.core.types import EventRecord
from wintermute.events.storage.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class ContextBundle:
    history: tuple[Message, ...]
    prompt: UserMessage
    references: tuple[UserMessage, ...] = ()


class ContextAssembler:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def assemble(self, trigger: EventRecord, *, exclude_run_id: str | None = None) -> ContextBundle:
        if trigger.layer not in ("conversation", "notification") or trigger.conversation_id is None:
            raise ValueError("Only conversation and notification events can trigger a run")
        history = self.store.sessions.history(
            trigger.conversation_id, trigger.user_id, exclude_run_id=exclude_run_id
        )
        if exclude_run_id is None:
            # 独立预览读取和调度器相同的待发差异；只有正式请求可确认这些活动。
            timeline = self.store.timeline.pending_timeline(
                trigger.conversation_id, trigger.user_id
            )
            if timeline is not None:
                history = (*history, timeline)
        return ContextBundle(history, self.store.sessions.input_message(trigger))
