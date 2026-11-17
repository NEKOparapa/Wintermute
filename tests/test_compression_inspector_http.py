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
        timeline_policy=TimelinePolicy(
            raw_tokens=12000, ai_threshold=20, summary_tokens=60, delivery_tokens=180
        ),
        clock=lambda: now,
    ) as store:
        gateway = EventGateway(store)
        http = HTTPInterface(settings, gateway, timeline_ai_enabled=True)
        sensor = gateway.perception.connect("work", conversation_id="work")
        original = {"title": "发布清单.md", "scroll": 42, "section": "验收要求" * 15}
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
            (slice_head,) = (await get(root, layer="slice"))["items"]
            assert slice_head["id"] != head["id"]
            assert (head["perception_count"], head["slice_count"]) == (1, 0)
            detail_path = root + "/" + head["id"]
            opened = await get(detail_path, limit="1")
            stages_path = f"/v1/history/perceptions/{observed.event.event_id}/stages"
            before_ai = await get(stages_path)
            assert before_ai["retained"]["value"] == original
            assert before_ai["parts"][0]["text"] == opened["rule_text"]
            assert before_ai["rounds"] == [] and before_ai["pending_size"] > before_ai["budget"]
            assert (await client.get(stages_path, headers=other)).status == 404
            assert opened["ai_text"] is None and "发布清单.md" in opened["rule_text"]
            raw = await get(root + "/sources/" + observed.event.event_id, activity="1")
            assert raw["items"][0]["payload"]["value"] == original
            assert raw["items"][0]["files"] == []
            assert opened["next_cursor"] is None
            slice_detail = await get(root + "/" + slice_head["id"])
            assert slice_detail["items"][0]["id"] == summary.item_id
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
            after_ai = await get(stages_path)
            (job,) = after_ai["rounds"]
            assert job["status"] == "ready" and job["summary"] == ai_output
            assert job["input"] == opened["rule_text"]
            assert after_ai["parts"][0]["round_id"] == job["id"]
            assert finished["rollup"]["rule_text"] == opened["rule_text"]
            assert store.history("work", "owner") == () and model.calls == 1

            def stamp(seconds):
                return (time + timedelta(seconds=seconds)).isoformat()

            value = {
                "foreground_app": "Editor",
                "started_at": stamp(0),
                "ended_at": stamp(10),
                "duration_seconds": 10,
                "work_objects": [
                    {"occurred_at": stamp(0), "window_title": "闪现"},
                    {"occurred_at": stamp(4), "window_title": "正文"},
                ],
            }
            visit = await sensor.observe(
                "desktop.window_span", value, occurred_at=time + timedelta(seconds=10)
            )
            trace = await get(f"/v1/history/perceptions/{visit.event.event_id}/stages")
            assert [(r["label"], r["seconds"], r["kept"]) for r in trace["decisions"]] == [
                ("Editor", 10, True),
                ("闪现", 4, False),
                ("正文", 6, True),
            ]
            assert "闪现" not in trace["parts"][0]["text"] and "正文" in trace["parts"][0]["text"]
            short = await sensor.observe(
                "desktop.window_span",
                {**value, "duration_seconds": 5},
                occurred_at=time + timedelta(seconds=11),
            )
            trace = await get(f"/v1/history/perceptions/{short.event.event_id}/stages")
            assert trace["dwell_threshold_seconds"] == 5
            assert trace["decisions"][0]["kept"] is False and trace["retained"] is None
            assert trace["parts"][0]["text"] == "" and trace["rounds"] == []

            # A quick switch to QQ is visible in the raw journal. Returning starts a new visit.
            async def upload(app, begin, end, objects):
                result = await client.post(
                    "/v1/perceptions",
                    json={
                        "event_id": f"switch-{begin}",
                        "state_key": "desktop.window_span",
                        "occurred_at": stamp(end),
                        "value": {
                            "foreground_app": app,
                            "started_at": stamp(begin),
                            "ended_at": stamp(end),
                            "duration_seconds": round(end - begin, 3),
                            "work_objects": objects,
                        },
                    },
                )
                assert result.status == 202
                return (await result.json())["event_id"]

            quick_id = await upload(
                "QQ", 20, 22, [{"occurred_at": stamp(20), "window_title": "QQ"}]
            )
            titles = [
                {"occurred_at": stamp(22), "window_title": "搜索", "ended_at": stamp(24)},
                {"occurred_at": stamp(26), "window_title": "搜索"},
                {"occurred_at": stamp(29), "window_title": "会议纪要"},
            ]
            editor_id = await upload("Editor", 22, 34.001, titles)
            only_id = await upload("Editor", 40, 45.001, [])
            history = {r["id"]: r for r in (await get("/v1/history", view="perception"))["items"]}
            assert history[quick_id]["raw"]["payload"]["value"]["duration_seconds"] == 2
            assert history[quick_id]["filter_result"] == "filtered"
            assert history[editor_id]["raw"]["payload"]["value"]["work_objects"] == titles
            assert history[editor_id]["filter_result"] == "partial"
            assert history[only_id]["filter_result"] == "kept"
            detail = await get(f"/v1/history/perceptions/{editor_id}/stages")
            assert [(r["seconds"], r["kept"]) for r in detail["decisions"]] == [
                (12.001, True),
                (2, False),
                (3, False),
                (5.001, True),
            ]
            assert detail["retained"]["value"]["work_objects"] == titles[-1:]
            assert (
                "会议纪要" in detail["parts"][0]["text"]
                and "搜索" not in detail["parts"][0]["text"]
            )

        await http.aclose()


async def test_perception_summary_timeline_sources_and_context_preview(tmp_path):
    now = datetime(2026, 11, 17, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai
    settings = HTTPSettings.model_validate(
        {
            "enabled": True,
            "terminals": [
                {"terminal_id": room, "user_id": "owner", "conversation_id": room, "token": room}
                for room in ("work", "other")
            ],
        }
    )
    profile = tmp_path / "profile.md"
    profile.write_text("# 用户档案\n\n## 长期习惯\n- 喜欢先看总结。\n", encoding="utf-8")

    class RollingSummary:
        calls = 0

        async def stream(self, context):
            self.calls += 1
            yield AssistantMessage(f"第{self.calls}轮摘要：用户在处理发布与编辑器。")

    with SQLiteStore(
        timeline_policy=TimelinePolicy(
            raw_tokens=12000, ai_threshold=10, summary_tokens=160, delivery_tokens=180
        ),
        clock=lambda: now + timedelta(minutes=10),
    ) as store:
        gateway = EventGateway(store)
        http = HTTPInterface(
            settings,
            gateway,
            timeline_ai_enabled=True,
            profile_files={"owner": profile},
            system_prompt="你是桌面助手。",
            context_input_limit=60000,
        )
        sensor = gateway.perception.connect("work", conversation_id="work")
        slice_ep = gateway.slice.connect("work", conversation_id="work")
        conv = gateway.conversation.connect("work", conversation_id="work")

        def stamp(sec: int) -> str:
            return (now + timedelta(seconds=sec)).isoformat()

        # 1) Short filtered visit
        await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "Toast",
                "started_at": stamp(0),
                "ended_at": stamp(3),
                "duration_seconds": 3,
                "work_objects": [],
            },
            occurred_at=now + timedelta(seconds=3),
        )
        # 2) Partial kept visit + 3) Full kept visit (exceeds delivery_tokens=160 -> round 1)
        titles = [
            {"occurred_at": stamp(10), "window_title": "闪现"},
            {"occurred_at": stamp(13), "window_title": "设计文档_" + ("A" * 60)},
        ]
        e1 = await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "Editor",
                "started_at": stamp(10),
                "ended_at": stamp(25),
                "duration_seconds": 15,
                "work_objects": titles,
            },
            occurred_at=now + timedelta(seconds=25),
        )
        e2 = await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "Terminal",
                "started_at": stamp(30),
                "ended_at": stamp(60),
                "duration_seconds": 30,
                "work_objects": [
                    {"occurred_at": stamp(30), "window_title": "pytest_" + ("B" * 60)}
                ],
            },
            occurred_at=now + timedelta(seconds=60),
        )
        worker = TimelineWorker(store, RollingSummary())
        assert await worker.process_next()  # round 1 ready
        # 4) Another visit rolling round 1 -> round 2
        e3 = await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "Browser",
                "started_at": stamp(70),
                "ended_at": stamp(120),
                "duration_seconds": 50,
                "work_objects": [
                    {"occurred_at": stamp(70), "window_title": "PR评审_" + ("C" * 80)}
                ],
            },
            occurred_at=now + timedelta(seconds=120),
        )
        assert await worker.process_next()  # round 2 ready
        # 5) Tail raw perception + 6) slice, both delivered with round 2 checkpoint
        e4 = await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "Notes",
                "started_at": stamp(130),
                "ended_at": stamp(145),
                "duration_seconds": 15,
                "work_objects": [],
            },
            occurred_at=now + timedelta(seconds=145),
        )
        await slice_ep.record("语音备注：明早九点发布。", occurred_at=now + timedelta(seconds=150))

        # Deliver via conversation run
        await conv.send("刚才我在忙什么？", occurred_at=now + timedelta(seconds=160))
        store.acquire_scheduler("runner")
        job = store.claim_next(defer_input=True)
        assert job is not None
        absorbed = store.absorb_session_inputs(job.job_id, messages=())
        from wintermute.agent.context.types import ContextCheckpoint, history_digest
        from wintermute.events.core.types import EventRecord, event_message

        prefix = tuple(
            event_message(item) if isinstance(item, EventRecord) else item for item in absorbed
        )
        reply = AssistantMessage("你刚刚在写设计文档、跑测试和看 PR。")
        store.record_model_read(
            job.job_id,
            len(prefix) + 1,
            ContextCheckpoint("历史会话概览", 1, history_digest(prefix[:1]), 1),
            messages=(*prefix, reply),
        )
        store.finish_job(
            job.job_id,
            messages=(*prefix, reply),
            response=reply.text,
            reply_terminal_id="work",
        )

        # 7) One newer buffered perception for preview
        e5 = await sensor.observe(
            "desktop.window_span",
            {
                "foreground_app": "Slack",
                "started_at": stamp(200),
                "ended_at": stamp(220),
                "duration_seconds": 20,
                "work_objects": [],
            },
            occurred_at=now + timedelta(seconds=220),
        )

        headers = {"X-Terminal-ID": "work", "Authorization": "Bearer work"}
        other = {"X-Terminal-ID": "other", "Authorization": "Bearer other"}
        async with TestClient(TestServer(http.create_app()), headers=headers) as client:

            async def get(path, **params):
                resp = await client.get(path, params=params)
                assert resp.status == 200 and "no-store" in resp.headers["Cache-Control"]
                return await resp.json()

            # Check stages enhancements: form='summary' vs 'raw', next_id on round 1
            s1 = await get(f"/v1/history/perceptions/{e1.event.event_id}/stages")
            assert s1["parts"][0]["delivery"]["form"] == "summary"
            assert s1["parts"][0]["delivery"]["run_id"] == job.job_id
            assert s1["parts"][0]["delivery"]["input_index"] == 0
            assert s1["rounds"][0]["next_id"] == s1["rounds"][0]["later"][0]["id"]

            s4 = await get(f"/v1/history/perceptions/{e4.event.event_id}/stages")
            assert s4["parts"][0]["delivery"]["form"] == "raw"

            # Check timeline/{id}/sources
            timeline_id = s4["parts"][0]["delivery"]["message"]["id"]
            assert timeline_id is not None
            sources = await get(f"/v1/history/timeline/{timeline_id}/sources")
            kinds = [p["kind"] for p in sources["parts"]]
            assert "perception_summary" in kinds
            assert "perception_line" in kinds
            assert "slice" in kinds
            summary_part = next(p for p in sources["parts"] if p["kind"] == "perception_summary")
            assert {m["event_id"] for m in summary_part["members"]} >= {
                e1.event.event_id,
                e2.event.event_id,
                e3.event.event_id,
            }
            raw_part = next(p for p in sources["parts"] if p["kind"] == "perception_line")
            assert raw_part["event_id"] == e4.event.event_id
            assert (
                await client.get(f"/v1/history/timeline/{timeline_id}/sources", headers=other)
            ).status == 404

            # Check perception/summary and history destination filtering
            summary = await get("/v1/history/perception/summary")
            assert summary["uploaded"] == 6
            assert summary["filtered"] == 1
            assert summary["kept"] == 5
            assert summary["partial"] == 1
            assert summary["round_count"] == 2
            assert summary["delivered_events"] == 4
            assert summary["buffered_events"] == 1
            assert summary["delivery_runs"] == 1

            filtered_list = await get("/v1/history", view="perception", destination="filtered")
            assert len(filtered_list["items"]) == 1
            buffered_list = await get("/v1/history", view="perception", destination="buffered")
            assert [item["id"] for item in buffered_list["items"]] == [e5.event.event_id]

            # Check context/preview (non-mutating: e5 remains buffered!)
            preview = await get("/v1/history/context/preview")
            segs = {seg["kind"]: seg for seg in preview["segments"]}
            assert segs["system"]["profile_present"] is True
            assert segs["system"]["base_bytes"] > 0 and segs["system"]["profile_bytes"] > 0
            assert segs["session_summary"]["text"] == "历史会话概览"
            assert segs["recent_messages"]["message_count"] >= 1
            assert "Slack" in segs["pending_timeline"]["text"]
            assert preview["input_limit"] == 60000
            assert preview["total_bytes"] > 0

            # Confirm preview did not mark e5 as read
            s5 = await get(f"/v1/history/perceptions/{e5.event.event_id}/stages")
            assert s5["parts"][0]["delivered"] is False

        await http.aclose()
