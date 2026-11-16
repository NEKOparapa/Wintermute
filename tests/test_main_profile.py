import asyncio

from wintermute.agent import AgentRunner, AssistantMessage
from wintermute.config.factory import create_scheduler
from wintermute.config.models import EventSettings
from wintermute.events import EventGateway, SQLiteStore, Terminal


async def test_chat_and_notification_get_current_users_profile_only_in_system_prompt(tmp_path):
    profile = tmp_path / "PROFILE.md"
    profile.write_text("## 长期偏好\n- 周会材料优先发手机。", encoding="utf-8")
    requests = []

    class Model:
        async def stream(self, context):
            requests.append(context)
            yield AssistantMessage("收到。")

    settings = EventSettings(profile_files={"owner": "PROFILE.md"}, poll_interval=0.01)
    base_prompt = "你是 Wintermute。"
    with SQLiteStore(":memory:") as store:
        gateway = EventGateway(store)
        for user in ("owner", "guest"):
            gateway.register_terminal(Terminal(terminal_id=user, user_id=user))
            gateway.register_conversation(user, user)
        scheduler = create_scheduler(
            settings,
            gateway,
            lambda _: AgentRunner(Model(), system_prompt=base_prompt),
            base_dir=tmp_path,
        )
        owner = gateway.conversation.connect("owner", "owner")
        guest = gateway.conversation.connect("guest", "guest")
        notice = gateway.notification.connect("owner", "owner", reply_terminal_id="owner")

        async def complete(submission):
            receipt = await submission
            assert (await scheduler.wait(receipt.job_id)).status == "completed"
            return requests[-1]

        async with asyncio.timeout(10), scheduler:
            first = await complete(owner.send("今天的议程准备好了吗？"))
            assert first.system_prompt.startswith(base_prompt)
            assert "周会材料优先发手机" in first.system_prompt
            assert len(first.messages) == 1 and "今天的议程准备好了吗" in first.messages[0].text
            other = await complete(guest.send("你好。"))
            assert other.system_prompt == base_prompt

            profile.write_text("## 长期偏好\n- 周会材料优先发电脑。", encoding="utf-8")
            updated = await complete(notice.publish("meeting.due", {"text": "评审即将开始。"}))
            assert "周会材料优先发电脑" in updated.system_prompt
            assert "手机" not in updated.system_prompt
            profile.unlink()
            assert (await complete(owner.send("谢谢。"))).system_prompt == base_prompt

        for context in requests:
            assert all("周会材料" not in message.text for message in context.messages)
        assert all("周会材料" not in m.text for m in store.history("owner", "owner"))
