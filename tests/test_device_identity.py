import asyncio
import base64
import hashlib
import json
import sqlite3
from contextlib import closing

from aiohttp import ClientSession
from test_chat_media import media_service

from wintermute.agent import ToolContext
from wintermute.application.agents import create_tools
from wintermute.config import AppConfig
from wintermute.events.core.types import Device, EventInput
from wintermute.events.storage.codec import encode_json
from wintermute.events.storage.sqlite import SQLiteStore


def test_device_id_upgrade_preserves_old_records_and_retries(tmp_path):
    path = tmp_path / "events.db"
    old = {
        "terminal_id": "laptop",
        "reply_terminal_id": "laptop",
        "conversation_id": "c",
        "source_id": "default",
        "source_seq": None,
        "correlation_id": None,
        "schema_version": 1,
        "producer_event_id": "before-upgrade",
        "layer": "conversation",
        "type": "user.message",
        "payload": {"text": "terminal_id", "terminal_id": "user data"},
    }
    fingerprint = hashlib.sha256(encode_json(old).encode()).hexdigest()
    current = {**old, "device_id": old["terminal_id"], "reply_device_id": old["reply_terminal_id"]}
    del current["terminal_id"], current["reply_terminal_id"]
    event = EventInput.model_validate(current)
    with SQLiteStore(path) as store:
        store.register_device(Device(device_id="laptop", user_id="u"))
        store.devices.observe("laptop", "test-installation", "Office")
        store.register_conversation("c", "u")
        event_id = store.append(event).event.event_id
    with closing(sqlite3.connect(path)) as db, db:
        for table in ("devices", "receipts", "client_devices"):
            db.execute(f"ALTER TABLE {table} RENAME COLUMN device_id TO terminal_id")
        db.execute("ALTER TABLE devices RENAME TO terminals")
        db.execute(
            "UPDATE terminals SET data=json_remove(json_set(data,'$.terminal_id',"
            "terminal_id),'$.device_id')"
        )
        db.execute(
            "UPDATE conversation_events SET data=json_remove(json_set(data,"
            "'$.terminal_id','laptop','$.reply_terminal_id','laptop'),"
            "'$.device_id','$.reply_device_id')"
        )
        db.execute("UPDATE receipts SET fingerprint=? WHERE event_id=?", (fingerprint, event_id))
        db.execute("PRAGMA user_version=35")
    for _ in range(2):
        with SQLiteStore(path) as store:
            assert store.get_device("laptop").user_id == "u"
            assert store.devices.names("u") == {"laptop": "Office"}
            saved = store.get_event(event_id)
            assert saved.device_id == saved.reply_device_id == "laptop"
            assert saved.payload == old["payload"]
            assert store.append(event).duplicate
            assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()


def test_old_config_reads_and_serializes_canonical_device_fields():
    config = AppConfig.model_validate(
        {
            "interfaces": {
                "http": {
                    "terminals": [
                        {
                            "terminal_id": "desk",
                            "user_id": "u",
                            "conversation_id": "c",
                            "token_env": "TEST",
                        }
                    ]
                }
            }
        }
    )
    http = config.interfaces.http.model_dump(mode="json")
    assert "terminals" not in http
    assert http["devices"][0]["device_id"] == "desk" and "terminal_id" not in http["devices"][0]


def test_main_workspace_tools(tmp_path):
    config = AppConfig.model_validate({"tools": {"cwd": "."}, "subagents": [{"name": "child"}]})
    tools = {t.name: t for t in create_tools(config.tools, base_dir=tmp_path)}
    assert set(tools) == {"read", "write", "edit", "exec"}
    assert config.subagents[0].tools.enabled == []

    async def invoke(name, **arguments):
        tool = tools[name]
        result = await tool.execute(tool.parameters.model_validate(arguments), ToolContext(name, 1))
        assert not result.is_error, result.content
        return result.content

    async def run():
        await invoke("write", path="note.txt", content="first")
        await invoke("edit", path="note.txt", edits=[{"old_text": "first", "new_text": "second"}])
        assert "second" in await invoke("read", path="note.txt")
        assert "exec-ok" in await invoke("exec", command="echo exec-ok")

    asyncio.run(run())


def test_device_identity_and_tool_routing(tmp_path):
    name, args = "device_statuses", {}
    answers = []
    answer_delivered = asyncio.Event()

    def reply(data):
        advertised = {tool["function"]["name"] for tool in data["tools"]}
        assert {
            "device_statuses",
            "device_messaging",
            "Mcp",
        } <= advertised
        assert (
            not {
                "terminal_tool",
                "call_device_connector",
                "list_user_devices",
                "ask_user",
                "notify_user",
            }
            & advertised
        )
        last = data["messages"][-1]
        if last["role"] == "tool":
            answers.append(json.loads(last["content"]))
            return {"delta": {"content": "done"}, "finish_reason": "stop"}
        if name is None:
            if "已选选项：周二" in json.dumps(last["content"], ensure_ascii=False):
                answer_delivered.set()
            return {"delta": {"content": "answer received"}, "finish_reason": "stop"}
        return {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"tool-{len(answers)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            },
            "finish_reason": "tool_calls",
        }

    def headers(device_id, title=None, instance=None):
        result = {
            "X-Device-ID": device_id,
            "Authorization": "Bearer " + device_id,
            "X-Device-Instance": instance or (device_id + "-installation"),
        }
        if title is not None:
            result["X-Device-Name"] = base64.b64encode(title.encode()).decode()
        return result

    async def call(client, device_id="desktop"):
        receipt = await (
            await client.post(
                "/v1/messages",
                headers=headers(device_id),
                json={"text": "query", "message_id": str(len(answers))},
            )
        ).json()
        for _ in range(150):
            job = await (
                await client.get("/v1/jobs/" + receipt["job_id"], headers=headers(device_id))
            ).json()
            if job["status"] in {"completed", "failed"}:
                assert job["status"] == "completed", job
                return answers[-1]
            await asyncio.sleep(0.05)
        raise AssertionError("job timed out")

    async def run():
        nonlocal name, args
        async with (
            media_service(tmp_path, model_reply=reply) as (url, _, _),
            ClientSession(url) as client,
        ):
            legacy_headers = {"Authorization": "Bearer desktop", "X-Terminal-ID": "desktop"}
            assert (await client.get("/v1/outputs", headers=legacy_headers)).status == 200
            assert (
                await client.get(
                    "/v1/outputs",
                    headers={
                        **legacy_headers,
                        "X-Device-ID": "esp32",
                    },
                )
            ).status == 401
            for device_id, title in [
                ("desktop", "工作电脑"),
                ("private", "另一会话的设备"),
                ("esp32", "别人的设备"),
            ]:
                assert (
                    await client.post("/v1/devices/self", headers=headers(device_id, title))
                ).status == 200
            assert (
                await client.post(
                    "/v1/devices/self", headers=headers("desktop", "冲突", "other-installation")
                )
            ).status == 409
            assert (
                await client.post("/v1/devices/self", headers=headers("desktop", "书房电脑"))
            ).status == 200
            # A previously issued poll may still carry the old name.
            assert (
                await client.get("/v1/outputs", headers=headers("desktop", "工作电脑"))
            ).status == 200
            assert (await call(client))["devices"] == [
                {
                    "device_id": "desktop",
                    "name": "书房电脑",
                    "status": "online",
                    "can_notify": True,
                },
                {"device_id": "mobile", "name": "mobile", "status": "unknown", "can_notify": True},
                {
                    "device_id": "private",
                    "name": "另一会话的设备",
                    "status": "online",
                    "can_notify": False,
                },
            ]
            assert (
                await client.post(
                    "/v1/terminal-tools/claim",
                    headers=headers("mobile", "我的手机"),
                    json={"tools": ["device_status"]},
                )
            ).status == 200
            name, args = "ConnectorList", {"device_id": "mobile", "id": "wintermute:device"}
            (phone,) = (await call(client))["devices"]
            (native,) = phone["connectors"]
            assert phone["device_id"] == "mobile" and native["authMode"] == "device"
            assert native["verified"] and native["connected"]
            provider = native["provider"]
            name, args = "Mcp", {"action": "search", "query": "device_status"}
            (found,) = (await call(client))["matches"]
            assert found["provider"] == provider
            args = {"action": "describe", "provider": provider, "tool": found["tool"]}
            assert (await call(client))["inputSchema"]["additionalProperties"] is False
            args = {"action": "call", "provider": provider, "tool": found["tool"], "args": {}}
            sent = (await call(client))["result"]
            claim = await (
                await client.post(
                    "/v1/terminal-tools/claim",
                    headers=headers("mobile"),
                    json={"tools": ["device_status"]},
                )
            ).json()
            assert claim["call"]["id"] == sent["id"] and sent["device_id"] == "mobile"
            lookup = {
                "action": "call",
                "provider": "wintermute:receipts",
                "tool": "get_call_result",
                "args": {"id": sent["id"]},
            }
            args = lookup
            assert (await call(client))["result"]["status"] == "running"
            assert "error" in await call(client, "private")
            assert "error" in await call(client, "esp32")
            result = await client.post(
                f"/v1/terminal-tools/{sent['id']}/result",
                headers=headers("mobile"),
                json={
                    "claim_token": claim["call"]["claim_token"],
                    "status": "succeeded",
                    "stdout": "battery=80",
                    "stderr": "",
                    "exit_code": 0,
                    "truncated": False,
                },
            )
            assert result.status == 200
            assert (await call(client))["result"]["stdout"] == "battery=80"
            await client.post(
                "/v1/terminal-tools/claim", headers=headers("mobile"), json={"tools": []}
            )
            args = {"action": "call", "provider": provider, "tool": "device_status", "args": {}}
            assert "error" in await call(client)
        name, args = "device_statuses", {}
        async with (
            media_service(tmp_path, model_reply=reply) as (url, _, _),
            ClientSession(url) as client,
        ):
            saved = (await call(client))["devices"]
            assert [(d["device_id"], d["name"]) for d in saved] == [
                ("desktop", "书房电脑"),
                ("mobile", "我的手机"),
                ("private", "另一会话的设备"),
            ]
            name, args = "Mcp", lookup
            assert (await call(client))["result"]["status"] == "succeeded"

            async def items(endpoint, device):
                response = await client.get("/v1/" + endpoint, headers=headers(device))
                assert response.status == 200
                return (await response.json())["items"]

            attachment = tmp_path / "brief.txt"
            attachment.write_text("original", encoding="utf-8")
            name, args = (
                "device_messaging",
                {
                    "action": "notify",
                    "targets": ["mobile"],
                    "title": "报告",
                    "body": "请查收",
                    "attachments": [f"{tmp_path.name}/brief.txt"],
                    "expires_at": "2099-01-01T12:00:00Z",
                },
            )
            receipt = await call(client)
            assert receipt["status"] == "queued" and receipt["attachment_count"] == 1
            for device in ("desktop", "private", "esp32"):
                assert await items("notifications", device) == []
            (notice,) = await items("notifications", "mobile")
            assert notice["id"] == receipt["id"] and notice["body"] == "请查收"
            attachment.write_text("changed", encoding="utf-8")
            path = f"/v1/notifications/{notice['id']}/files/{notice['attachments'][0]['id']}"
            assert await (await client.get(path, headers=headers("mobile"))).read() == b"original"
            assert (await client.get(path, headers=headers("desktop"))).status == 404
            ack = await client.post(
                f"/v1/notifications/{notice['id']}/read", headers=headers("mobile")
            )
            assert ack.status == 200 and (await ack.json())["read_at"]

            args = {
                "action": "ask",
                "title": "排期",
                "question": "选哪天？",
                "options": ["周一", "周二"],
                "allow_custom": False,
            }
            question = await call(client)
            assert question["status"] == "pending"
            for device in ("desktop", "mobile"):
                (card,) = await items("questions", device)
                assert card["id"] == question["id"] and card["options"] == args["options"]
            for device in ("private", "esp32"):
                assert await items("questions", device) == []
            name = None
            response = await client.post(
                f"/v1/questions/{question['id']}/answer",
                headers=headers("mobile"),
                json={"selected_options": ["周二"]},
            )
            assert response.status == 200
            answer = await response.json()
            assert answer["answered_device_id"] == "mobile" and answer["status"] == "answered"
            assert await items("questions", "desktop") == []
            await asyncio.wait_for(answer_delivered.wait(), timeout=10)

    asyncio.run(run())
