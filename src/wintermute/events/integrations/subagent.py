"""将后台子 Agent 的终态报告发布到通知层。"""

from dataclasses import asdict

from wintermute.agent.tools.subagent.background import BackgroundTaskState
from wintermute.events.ingress.gateway import TerminalEndpoint


class BackgroundEventPublisher:
    """每个会话绑定一个通知终端，任务结果使用稳定的幂等键。"""

    def __init__(self, endpoint: TerminalEndpoint) -> None:
        if endpoint.layer != "notification" or endpoint.conversation_id is None:
            raise ValueError("Background results require a bound notification endpoint")
        self.endpoint = endpoint

    async def __call__(self, state: BackgroundTaskState) -> None:
        if state.status not in {"completed", "error", "cancelled"}:
            raise ValueError("Only terminal background task states can be published")
        payload = asdict(state)
        # 传输过程自己的状态不属于子任务结果，重试时不能改变事件内容。
        payload.pop("notification_status")
        payload.pop("notification_error")
        await self.endpoint.publish(
            f"subagent.{state.status}",
            payload,
            producer_event_id=f"background:{state.task_id}",
            correlation_id=state.task_id,
        )
