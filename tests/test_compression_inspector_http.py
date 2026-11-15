"""The desktop inspector shows source material and the stored results without starting jobs."""

from datetime import UTC, datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from wintermute.agent import AssistantMessage
from wintermute.config.models import HTTPSettings
from wintermute.events import EventGateway, SQLiteStore
from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.runtime.timeline_worker import TimelineWorker
from wintermute.interfaces.http import HTTPInterface


async def test_inspect_sources_and_cached_rule_and_ai_through_http():
    time = datetime(2026, 11, 15, 3, 20, tzinfo=UTC)
    now = time
    settings = HTTPSettings.model_validate(
        {
            "enabled": True,
            "terminals": [
                {"terminal_id": room, "user_id": "owner", "conversation_id": room, "token": room}
                for room in ("work", "other")
            ],
        }
    )
    ai_output = "用户查看了发布清单，会议确认周五交付。"

    class Summary:
        calls = 0

        async def stream(self, context):
            self.calls += 1
            yield AssistantMessage(ai_output)

    with SQLiteStore(
        timeline_policy=TimelinePolicy(raw_tokens=12000, ai_threshold=20, summary_tokens=60),
        clock=lambda: now,
    ) as store:
        gateway = EventGateway(store)
        http = HTTPInterface(settings, gateway, timeline_ai_enabled=True)
        sensor = gateway.perception.connect("work", conversation_id="work")
        original = {"title": "发布清单.md", "scroll": 42}
        observed = await sensor.observe("active_document", original, occurred_at=time)
        endpoint = gateway.slice.connect("work", conversation_id="work")
        a = await endpoint.record("原话一：改到周五。", occurred_at=time)
        b = await endpoint.record("原话二：周五交付。", occurred_at=time)
        summary = SliceStreamItem(
            item_id="fixture-summary",
            user_id="owner",
            conversation_id="work",
            terminal_id="work",
            source_id="default",
            day=time.date().isoformat(),
            timezone="UTC",
            started_at=time,
            ended_at=time,
            created_at=time,
            source_event_ids=(a.event.event_id, b.event.event_id),
            strategy="rule",
            text="会议确认周五交付。",
        )
        store.commit_slice(summary)
        model = Summary()
        root = "/v1/history/compression"
        headers = {"X-Terminal-ID": "work", "Authorization": "Bearer work"}
        other = {"X-Terminal-ID": "other", "Authorization": "Bearer other"}
        async with TestClient(TestServer(http.create_app()), headers=headers) as client:

            async def get(path, **params):
                response = await client.get(path, params=params)
                assert response.status == 200 and "no-store" in response.headers["Cache-Control"]
                return await response.json()

            (head,) = (await get(root, layer="perception"))["items"]
            assert head in (await get(root, layer="slice"))["items"]
            assert (head["perception_count"], head["slice_count"], head["status"]) == (1, 1, "open")
            detail_path = root + "/" + head["id"]
            opened = await get(detail_path, limit="1")
            assert opened["ai_text"] is None and "发布清单.md" in opened["rule_text"]
            raw = await get(root + "/sources/" + observed.event.event_id, activity="1")
            assert raw["items"][0]["payload"]["value"] == original
            assert raw["items"][0]["files"] == []
            next_page = await get(detail_path, cursor=opened["next_cursor"], limit="1")
            assert next_page["items"][0]["id"] == summary.item_id
            src_path = root + "/sources/" + summary.item_id
            page = await get(src_path, limit="1", activity="1")
            assert page["items"][0]["payload"]["text"] == "原话一：改到周五。"
            assert page["extraction"]["text"] == summary.text
            end = await get(src_path, limit="1", activity="1", cursor=page["next_cursor"])
            assert end["items"][0]["payload"]["text"] == "原话二：周五交付。"
            assert (await client.get(detail_path, headers=other)).status == 404
            assert (await client.get(src_path, headers=other)).status == 404
            assert (await client.get(root, params={"layer": "chat"})).status == 400

            now += timedelta(minutes=16)
            worker = TimelineWorker(store, model)
            assert model.calls == 0 and await worker.process_next()
            finished = await get(detail_path)
            assert finished["batch"]["status"] == "ready" and finished["ai_text"] == ai_output
            assert finished["rule_text"] == opened["rule_text"]
            assert store.history("work", "owner") == () and model.calls == 1
        await http.aclose()
