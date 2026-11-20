import asyncio
import json
import sqlite3

import pytest
from aiohttp import ClientSession
from test_chat_media import media_service

from wintermute.agent import ToolContext
from wintermute.agent.tools.connectors import connector_list_tool
from wintermute.agent.tools.connectors.device_arguments import (
    device_tool_info,
    validate_device_arguments,
)
from wintermute.events.integrations import devices
from wintermute.events.integrations.mcp_client import McpToolInfo, StreamableHttpMcpClient
from wintermute.events.storage.repositories.connectors import ConnectorSource
from wintermute.events.storage.sqlite import SQLiteStore


def test_android_tool_contracts():
    args = {
        "android_set_alarm": {"hour": 7, "minute": 15},
        "android_start_timer": {"seconds": 1500},
        "android_app_usage": {},
        "android_calendar_events": {"start_ms": 1000, "end_ms": 2000},
        "android_calendar_create": {"start_ms": 1000, "end_ms": 2000, "title": "评审"},
        "android_contacts": {"query": "陈"},
        "android_files_list": {},
        "android_files_read": {"path": "工作/清单.txt"},
        "android_location": {},
        "android_notifications": {},
        "android_sms": {"query": "快递"},
    }
    for name, example in args.items():
        assert device_tool_info(name).input_schema["additionalProperties"] is False
        validate_device_arguments(name, example)
        with pytest.raises(ValueError):
            validate_device_arguments(name, {**example, "unexpected": True})
    for name, invalid in (
        ("android_start_timer", {"seconds": 0}),
        ("android_sms", {"query": "  "}),
        ("android_calendar_events", {"start_ms": 2000, "end_ms": 1000}),
    ):
        with pytest.raises(ValueError):
            validate_device_arguments(name, invalid)


def test_ios_device_contracts():
    window = {"start_ms": 1000, "end_ms": 2000}
    examples = {
        "ios_calendar_events": window,
        "ios_calendar_create": {**window, "title": "会议"},
        "ios_contacts": {"query": "陈"},
        "ios_health_summary": {"days_ago": 1},
        "ios_music_search": {"query": "jazz"},
        "ios_music_control": {"action": "seek", "seconds": 80},
        "ios_photos_search": {**window, "kind": "screenshot"},
        "ios_photo_open": {"id": "selected-item"},
        "ios_reminders_list": {},
        "ios_reminders_create": {"title": "取快递", "due_ms": 100000},
        "ios_reminders_complete": {"id": "reminder"},
        "ios_location": {},
    }
    for name, example in examples.items():
        assert device_tool_info(name).description != "设备上报的工具"
        validate_device_arguments(name, example)
        with pytest.raises(ValueError):
            validate_device_arguments(name, {**example, "extra": True})
    for name, arguments in [
        ("ios_health_summary", {"days_ago": 7}),
        ("ios_music_control", {"action": "play"}),
        ("ios_music_control", {"action": "pause", "seconds": 30}),
        ("ios_photos_search", {**window, "kind": "unknown"}),
        ("ios_reminders_create", {"title": "  "}),
    ]:
        with pytest.raises(ValueError):
            validate_device_arguments(name, arguments)


def test_mac_device_contracts():
    for name, args in (
        ("mac_finder_selection", {}),
        ("mac_notes_create", {"title": "交接", "body": "更新进度"}),
        ("mac_music_control", {"action": "resume"}),
        ("mac_speak", {"text": "会议将在五分钟后开始"}),
    ):
        assert device_tool_info(name).input_schema["additionalProperties"] is False
        validate_device_arguments(name, args)
        with pytest.raises(ValueError):
            validate_device_arguments(name, {**args, "extra": True})
    for name, args in (
        ("mac_notes_create", {"title": "  ", "body": "会议"}),
        ("mac_music_control", {"action": "seek", "seconds": 10}),
        ("mac_speak", {"text": "字" * 201}),
    ):
        with pytest.raises(ValueError):
            validate_device_arguments(name, args)


def test_legacy_device_connector_migrates_once(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with SQLiteStore(path) as store:
        source = ConnectorSource(kind="official", found_at="https://example.com", publisher="test")
        account = store.connectors.upsert_mcp(
            "u", name="Docs", endpoint="https://example.com/mcp", source=source
        )
        store.connectors.save_credentials("u", account.id, {"token": "local-test"})
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("DROP TABLE device_connectors")
            db.execute(
                "INSERT INTO connectors(user_id,id,name,tool_type,auth_mode,device_id,config_json,"
                "enabled,verified,created_at,updated_at) VALUES "
                """('u','obsidian','Original','device','device','a','{"vault":"old"}',1,1,100,200)"""
            )
    finally:
        db.close()

    with SQLiteStore(path) as store:
        old = store.connectors.get("u", "obsidian", device_id="a")
        assert (
            old.name,
            old.config,
            old.enabled,
            old.created_at.timestamp(),
            old.updated_at.timestamp(),
        ) == ("Original", {"vault": "old"}, True, 100, 200)
        store.connectors.upsert_device(
            "u", connector_id="obsidian", device_id="b", name="Other", enabled=False
        )
        store.connectors.delete("u", "obsidian", device_id="a")
    with SQLiteStore(path) as store:
        assert [
            (r.device_id, r.name, r.enabled) for r in store.connectors.list_devices("u", ("a", "b"))
        ] == [("b", "Other", False)]
        assert store.connectors.get_credentials("u", account.id) == (
            "default",
            {"token": "local-test"},
        )


def test_connector_list_reports_account_authorization(tmp_path):
    path = tmp_path / "permissions.sqlite"
    with SQLiteStore(path) as store:
        store.register_conversation("c", "u")
        source = ConnectorSource(kind="official", found_at="https://example.com", publisher="test")
        for name in ("Docs", "Slack"):
            store.connectors.upsert_mcp(
                "u",
                name=name,
                endpoint=f"https://example.com/{name}",
                source=source,
                header_names=["Authorization"],
            )
    # Seed recorded OAuth permissions in the same format used by persisted catalogs.
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE connectors SET auth_mode='oauth', verified=1, scopes=?, grants=? "
            "WHERE user_id='u' AND id='slack'",
            (json.dumps(["read"]), json.dumps({"bot": ["chat:write"], "user": ["files:read"]})),
        )

    with SQLiteStore(path) as store:
        tool = connector_list_tool(store, "c", (), None)

        def query(**kwargs):
            args = tool.parameters.model_validate(kwargs, strict=True)
            result = asyncio.run(tool.execute(args, ToolContext("list", 1)))
            assert not result.is_error
            return {rec["id"]: rec for rec in json.loads(result.content)["accountConnectors"]}

        docs = query(id="docs")["docs"]
        assert (docs["authMode"], docs["verified"], docs["status"]) == (
            "credential",
            False,
            "pending_auth",
        )
        (slack,) = query(id="slack", scopes=["read", "write"]).values()
        assert slack["authMode"] == "oauth" and slack["verified"]
        assert slack["missingScopes"] == ["write"]
        assert not slack["connected"] and slack["status"] == "missing_scopes"
        grants = {"bot": ["chat:write", "files:read"], "user": ["files:read", "chat:write"]}
        records = query(requestedGrants=grants)
        assert records["docs"] == docs
        assert records["slack"]["missingGrants"] == {"bot": ["files:read"], "user": ["chat:write"]}
        assert records["slack"]["status"] == "missing_grants" and not records["slack"]["connected"]
        slack = query(id="slack", scopes=["read"], requestedGrants={"bot": ["chat:write"]})["slack"]
        assert slack["missingScopes"] == [] and slack["missingGrants"] == {"bot": [], "user": []}
        assert slack["status"] == "connected" and slack["connected"]
        assert query(id="missing") == {} and query(device_id="missing", id="slack") == {}


def test_grouped_connector_list_over_http(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(devices, "monotonic", lambda: clock[0])
    results = []
    tool_name, arguments = "ConnectorList", {}

    async def list_account_tools(self, connector, creds):
        assert connector.id == "docs"
        return [McpToolInfo("lookup", "Search the docs", {"type": "object", "properties": {}})]

    async def call_account_tool(self, connector, name, args, creds):
        assert connector.id == "docs" and name == "lookup" and args == {}
        assert creds == {"token": "second-test-token"}
        return {"content": [{"type": "text", "text": "second account"}]}

    monkeypatch.setattr(StreamableHttpMcpClient, "list_tools", list_account_tools)
    monkeypatch.setattr(StreamableHttpMcpClient, "call_tool", call_account_tool)

    def reply(data):
        tools = {tool["function"]["name"] for tool in data["tools"]}
        assert "ConnectorList" in tools and "ConnectorStatus" not in tools
        last = data["messages"][-1]
        if last["role"] == "tool":
            results.append(json.loads(last["content"]))
            return {"delta": {"content": "ok"}, "finish_reason": "stop"}
        return {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "type": "function",
                        "id": f"call-{len(results)}",
                        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                    }
                ]
            },
            "finish_reason": "tool_calls",
        }

    async def check():
        nonlocal tool_name, arguments
        with SQLiteStore(tmp_path / "events.db") as store:
            store.connectors.upsert_mcp(
                "u",
                name="Docs",
                endpoint="https://example.com/mcp",
                header_names=["Authorization"],
                source=ConnectorSource(
                    kind="official", found_at="https://example.com", publisher="test"
                ),
            )
            for account in ("first", "second"):
                store.connectors.save_credentials(
                    "u",
                    "docs",
                    {"token": f"{account}-test-token"},
                    connection_id=account,
                    verified=True,
                )
        async with (
            media_service(tmp_path, model_reply=reply) as (url, _, _),
            ClientSession(url) as client,
        ):

            def auth(device):
                return {"Authorization": f"Bearer {device}", "X-Device-ID": device}

            async def post(device, route, body):
                async with client.post(route, json=body, headers=auth(device)) as response:
                    assert 200 <= response.status < 300, await response.text()
                    return await response.json()

            async def sync(device, enabled):
                return await post(
                    device,
                    "/v1/connectors/device-sync",
                    {
                        "id": "obsidian",
                        "name": f"Vault {device}",
                        "enabled": enabled,
                        "config": {"vaultName": device},
                    },
                )

            async def call(name="ConnectorList", **kwargs):
                nonlocal tool_name, arguments
                tool_name, arguments = name, kwargs
                receipt = await post(
                    "desktop", "/v1/messages", {"text": "query", "message_id": str(len(results))}
                )
                for _ in range(150):
                    job = await (
                        await client.get("/v1/jobs/" + receipt["job_id"], headers=auth("desktop"))
                    ).json()
                    if job["status"] in {"completed", "failed"}:
                        assert job["status"] == "completed", job
                        return results[-1]
                    await asyncio.sleep(0.05)
                raise AssertionError("job timed out")

            first = await call()
            assert [(d["device_id"], d["status"], d["connectors"]) for d in first["devices"]] == [
                ("desktop", "online", []),
                ("mobile", "unknown", []),
            ]
            assert [r["id"] for r in first["accountConnectors"]] == ["docs"]
            docs = first["accountConnectors"][0]
            assert docs["authMode"] == "credential" and docs["verified"] and docs["connected"]
            assert [(c["connectionId"], c["verified"]) for c in docs["connections"]] == [
                ("first", True),
                ("second", True),
            ]
            assert "test-token" not in json.dumps(docs)
            checked = await call(id="docs", scopes=["docs.read"])
            assert checked["devices"] == []
            (restricted,) = checked["accountConnectors"]
            assert restricted["missingScopes"] == ["docs.read"] and not restricted["connected"]
            assert restricted["status"] == "missing_scopes" and restricted["verified"]
            assert await call(device_id="mobile") == {
                "devices": [first["devices"][1]],
                "accountConnectors": [],
            }
            await asyncio.gather(
                *(sync(d, True) for d in ("desktop", "mobile", "private", "esp32"))
            )
            for d in ("desktop", "mobile"):
                await post(d, "/v1/terminal-tools/claim", {"tools": ["obsidian"]})
            active = (await call())["devices"]
            assert [d["device_id"] for d in active] == ["desktop", "mobile"]
            async with client.get("/v1/connectors", headers=auth("desktop")) as response:
                assert response.status == 200
                settings = (await response.json())["items"]
                assert [(r["id"], r["device_id"]) for r in settings] == [
                    ("docs", None),
                    ("obsidian", "desktop"),
                    ("obsidian", "mobile"),
                ]
            async with client.post(
                "/v1/connectors/obsidian/test?device_id=mobile", headers=auth("desktop")
            ) as response:
                assert response.status == 200
                assert (await response.json())["item"]["config"] == {"vaultName": "mobile"}
            for d in active:
                (app,) = d["connectors"]
                assert app["id"] == "obsidian" and app["name"] == "Vault " + d["device_id"]
                assert app["enabled"] and app["connected"] and d["status"] == "online"
                assert app["authMode"] == "device" and app["verified"]

            async def mcp(action, **params):
                return await call("Mcp", action=action, **params)

            found = await mcp("search", query="obsidian")
            providers = [row["provider"] for row in found["matches"]]
            assert providers == [d["connectors"][0]["provider"] for d in active]
            assert providers == ["obsidian@desktop", "obsidian@mobile"]
            assert len((await mcp("search", query="obsidian", limit=1))["matches"]) == 1
            ambiguous = await mcp("call", provider="obsidian", tool="obsidian", args={})
            assert ambiguous["providers"] == providers
            assert "error" in await mcp("list", provider="obsidian@private")
            assert "error" in await mcp("list", provider="obsidian@esp32")
            target = providers[1]
            listed = await mcp("list", provider=target)
            tool = listed["tools"][0]["tool"]
            assert tool == "obsidian" and listed["provider"] == target
            params = {"provider": target, "tool": tool}
            assert "error" in await mcp("list", provider=target, connectionId="first")
            assert (await mcp("describe", provider=target, tool="guess"))["availableTools"] == [
                tool
            ]
            schema = (await mcp("describe", **params))["inputSchema"]
            for invalid in ({}, {"action": "read_note"}, {"action": "vault_info", "extra": 1}):
                error = await mcp("call", args=invalid, **params)
                assert error["inputSchema"] == schema and "error" in error
            correct = {"action": "read_note", "path": "test.md"}
            receipt = (await mcp("call", args=correct, **params))["result"]
            assert receipt["status"] == "queued" and receipt["device_id"] == "mobile"
            route = "/v1/terminal-tools/claim"
            assert (await post("desktop", route, {"tools": [tool]}))["call"] is None
            actual = (await post("mobile", route, {"tools": [tool]}))["call"]
            assert actual["arguments"] == correct and actual["id"] == receipt["id"]
            await post(
                "mobile",
                f"/v1/terminal-tools/{actual['id']}/result",
                {
                    "claim_token": actual["claim_token"],
                    "status": "succeeded",
                    "stdout": "note text",
                    "stderr": "",
                    "exit_code": 0,
                    "truncated": False,
                },
            )
            async with client.get("/v1/connectors/history", headers=auth("desktop")) as response:
                item = (await response.json())["items"][0]
                assert (item["status"], item["target"], item["output"]) == (
                    "succeeded",
                    "mobile",
                    "note text",
                )
            assert (await mcp("search", query="lookup"))["matches"][0]["provider"] == "docs"
            cloud = await mcp(
                "call", provider="docs", tool="lookup", connectionId="second", args={}
            )
            assert cloud["result"]["content"][0]["text"] == "second account"
            assert "error" in await mcp(
                "call", provider="docs", tool="lookup", connectionId="missing", args={}
            )

            clock[0] += 20
            await sync("desktop", False)
            for provider in providers:
                assert "error" in await mcp("call", provider=provider, tool=tool, args=correct)
            desktop, mobile = (await call())["devices"]
            assert desktop["status"] == "online" and not desktop["connectors"][0]["enabled"]
            assert mobile["status"] == "offline" and mobile["connectors"][0]["enabled"]
            assert not mobile["connectors"][0]["connected"]
            assert await call(id="obsidian") == {
                "devices": [desktop, mobile],
                "accountConnectors": [],
            }
            assert await call(id="obsidian", device_id="mobile") == {
                "devices": [mobile],
                "accountConnectors": [],
            }
            assert await call(id="docs") == {"devices": [], "accountConnectors": [docs]}
            empty = {"devices": [], "accountConnectors": []}
            for device_id in ("private", "esp32", "missing"):
                assert await call(id="obsidian", device_id=device_id) == empty
            assert await call(device_id="desktop", id="docs") == empty
            assert await call(id="missing") == empty
            assert (
                await client.delete(
                    "/v1/connectors/obsidian?device_id=private", headers=auth("desktop")
                )
            ).status == 404
            assert (
                await client.delete("/v1/connectors/obsidian", headers=auth("desktop"))
            ).status == 200
            assert (
                await client.delete("/v1/connectors/obsidian", headers=auth("desktop"))
            ).status == 404
            desktop, mobile = (await call())["devices"]
            assert not desktop["connectors"] and mobile["connectors"][0]["name"] == "Vault mobile"
            assert (await call(id="obsidian"))["devices"] == [mobile]

    asyncio.run(check())
