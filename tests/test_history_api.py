from datetime import UTC, datetime

from wintermute.agent import AssistantMessage
from wintermute.events import EventGateway, SQLiteStore, Terminal


async def test_layer_journals_page_only_ingress_while_dialogue_keeps_replies() -> None:
    with SQLiteStore() as store:
        gateway = EventGateway(store)
        gateway.register_terminal(Terminal(terminal_id="desk", user_id="owner"))
        gateway.register_conversation("room", "owner")
        talk = gateway.conversation.connect("desk", "room")
        notices = gateway.notification.connect("desk", "room", reply_terminal_id="desk")
        instant = datetime(2026, 11, 18, tzinfo=UTC)
        store.acquire_scheduler("worker")
        inputs: dict[str, list[str]] = {"conversation": [], "notification": []}
        replies = []
        for i in range(2):
            for endpoint in (talk, notices):
                if endpoint is talk:
                    receipt = await talk.send(f"question {i}", occurred_at=instant)
                else:
                    receipt = await notices.publish(
                        "reminder", {"text": f"notice {i}"}, occurred_at=instant
                    )
                inputs[endpoint.layer].append(receipt.event.event_id)
                job = store.claim_next()
                assert job is not None and job.job_id == receipt.job_id
                text = f"reply to {endpoint.layer} {i}"
                output = store.finish_job(
                    job.job_id,
                    messages=(*store.get_run_messages(job.job_id), AssistantMessage(text)),
                    response=text,
                    reply_terminal_id="desk",
                )
                replies.append(output.output_id)

        scope = {"user_id": "owner", "conversation_id": "room"}
        for layer, (older, newer) in inputs.items():
            first, cursor = store.browse_history(**scope, view=layer, limit=1)
            assert [item.id for item in first] == [newer]
            assert cursor is not None
            second, end = store.browse_history(**scope, view=layer, cursor=cursor, limit=1)
            assert [item.id for item in second] == [older] and end is None
            # Old clients may resume from an output (part 1) just before its input (part 0).
            old_cursor = cursor.rsplit(":", 1)[0] + ":1"
            resumed, end = store.browse_history(**scope, view=layer, cursor=old_cursor)
            assert [item.id for item in resumed] == [newer, older] and end is None

        dialogue, _ = store.browse_history(**scope, view="dialogue")
        assert {item.id for item in dialogue} == set(inputs["conversation"] + replies)
