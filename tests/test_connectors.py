"""Tests for connector storage, 5 connector meta-tools, and HTTP connector routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from wintermute.agent import ToolContext
from wintermute.config.models import HTTPSettings, HTTPTerminalSettings
from wintermute.events import EventGateway, SQLiteStore
from wintermute.events.integrations.connectors import ConnectorManager, create_connector_tools
from wintermute.events.integrations.mcp_client import (
    McpAuthError,
    McpToolInfo,
    StreamableHttpMcpClient,
)
from wintermute.events.integrations.terminal_tool import TerminalCapabilities
from wintermute.events.storage.connectors import ConnectorRecord
from wintermute.interfaces.http import HTTPInterface


class FakeMcpClient(StreamableHttpMcpClient):
    def __init__(self, *, required_key: str | None = "secret-token-123") -> None:
        super().__init__()
        self.required_key = required_key
        self.calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    def _check_auth(self, credentials: Mapping[str, str] | None) -> None:
        if self.required_key is None:
            return
        if not credentials or self.required_key not in credentials.values():
            raise McpAuthError("Invalid token")

    async def list_tools(
        self,
        connector: ConnectorRecord,
        credentials: Mapping[str, str] | None = None,
    ) -> list[McpToolInfo]:
        self._check_auth(credentials)
        return [
            McpToolInfo(
                name="create_issue",
                description="Create a GitHub issue in a repository",
                input_schema={
                    "type": "object",
                    "required": ["repo", "title"],
                    "properties": {
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            ),
            McpToolInfo(
                name="list_repos",
                description="List repositories",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    async def call_tool(
        self,
        connector: ConnectorRecord,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self._check_auth(credentials)
        self.calls.append((tool_name, dict(arguments), dict(credentials) if credentials else None))
        return {
            "content": [{"type": "text", "text": f"ok:{tool_name}:{arguments.get('title', '')}"}]
        }


def _ctx(call_id: str = "call-1") -> ToolContext:
    return ToolContext(call_id=call_id, turn=1, run_id="run-1")


def test_five_connector_tools_end_to_end() -> None:
    async def run() -> None:
        store = SQLiteStore(":memory:")
        store.register_conversation("conv-1", "user-1")
        binding = HTTPTerminalSettings(
            terminal_id="http-desktop",
            user_id="user-1",
            conversation_id="conv-1",
            token="desktop-token-1",
        )
        caps = TerminalCapabilities()
        caps.update("http-desktop", ["obsidian"])
        fake_mcp = FakeMcpClient()
        manager = ConnectorManager(mcp_client=fake_mcp)
        tools = {
            t.name: t
            for t in create_connector_tools(store, "conv-1", [binding], caps, manager)
        }
        assert set(tools) == {
            "ConnectorList",
            "ConnectorStatus",
            "ConnectorSetup",
            "ConnectorCredentialSave",
            "Mcp",
        }

        # Register Obsidian device connector
        store.connectors.upsert_device(
            "user-1",
            connector_id="obsidian",
            name="Obsidian",
            device_id="http-desktop",
            enabled=True,
            config={"vaultName": "MyVault"},
        )

        # 1. ConnectorSetup rejects non-HTTPS
        bad_setup = await tools["ConnectorSetup"].execute(
            tools["ConnectorSetup"].parameters.model_validate(
                {
                    "endpoint": "http://example.com/mcp",
                    "name": "GitHub",
                    "source": {
                        "kind": "official",
                        "foundAt": "https://docs.github.com/mcp",
                        "publisher": "GitHub",
                    },
                }
            ),
            _ctx(),
        )
        assert bad_setup.is_error

        # 2. ConnectorSetup with credentials needed returns pending_auth without secrets
        setup_res = await tools["ConnectorSetup"].execute(
            tools["ConnectorSetup"].parameters.model_validate(
                {
                    "endpoint": "https://mcp.github.example/v1",
                    "name": "GitHub",
                    "source": {
                        "kind": "official",
                        "foundAt": "https://docs.github.com/mcp",
                        "publisher": "GitHub",
                    },
                    "headerNames": ["Authorization"],
                    "credentialLabel": "Personal Access Token",
                }
            ),
            _ctx(),
        )
        assert not setup_res.is_error
        setup_data = json.loads(setup_res.content)
        assert setup_data["status"] == "pending_auth"
        assert setup_data["id"] == "github"

        # 3. ConnectorList shows obsidian (connected device) and github (pending_auth mcp)
        list_res = await tools["ConnectorList"].execute(
            tools["ConnectorList"].parameters.model_validate({}),
            _ctx(),
        )
        items = {c["id"]: c for c in json.loads(list_res.content)["connectors"]}
        assert items["obsidian"]["toolType"] == "device"
        assert items["obsidian"]["connected"] is True
        assert items["github"]["toolType"] == "mcp"
        assert items["github"]["connected"] is False

        # 4. ConnectorCredentialSave validates immediately and never echoes secret
        cred_res = await tools["ConnectorCredentialSave"].execute(
            tools["ConnectorCredentialSave"].parameters.model_validate(
                {
                    "connectorId": "github",
                    "values": {"Authorization": "secret-token-123"},
                }
            ),
            _ctx(),
        )
        assert not cred_res.is_error
        assert "secret-token-123" not in cred_res.content
        assert json.loads(cred_res.content)["verified"] is True

        # 5. ConnectorStatus reports status only (not tools), and supports scopes/grants
        status_res = await tools["ConnectorStatus"].execute(
            tools["ConnectorStatus"].parameters.model_validate(
                {"id": "github", "scopes": ["repo"]}
            ),
            _ctx(),
        )
        status_data = json.loads(status_res.content)
        assert status_data["status"] == "missing_scopes"
        assert status_data["missingScopes"] == ["repo"]
        assert "tools" not in status_data

        # 6. Mcp search / list / describe / call (with schema error feedback)
        search_res = await tools["Mcp"].execute(
            tools["Mcp"].parameters.model_validate({"action": "search", "query": "issue"}),
            _ctx(),
        )
        matches = json.loads(search_res.content)["matches"]
        assert len(matches) == 1
        assert matches[0]["provider"] == "github"
        assert matches[0]["tool"] == "create_issue"

        desc_res = await tools["Mcp"].execute(
            tools["Mcp"].parameters.model_validate(
                {"action": "describe", "provider": "github", "tool": "create_issue"}
            ),
            _ctx(),
        )
        assert "inputSchema" in json.loads(desc_res.content)

        # Invalid args returns schema so model can retry
        bad_call = await tools["Mcp"].execute(
            tools["Mcp"].parameters.model_validate(
                {
                    "action": "call",
                    "provider": "github",
                    "tool": "create_issue",
                    "args": {"repo": "owner/repo"},
                }
            ),
            _ctx(),
        )
        assert bad_call.is_error
        assert "inputSchema" in json.loads(bad_call.content)

        # Valid call succeeds
        good_call = await tools["Mcp"].execute(
            tools["Mcp"].parameters.model_validate(
                {
                    "action": "call",
                    "provider": "github",
                    "tool": "create_issue",
                    "args": {"repo": "owner/repo", "title": "Hello"},
                }
            ),
            _ctx(),
        )
        assert not good_call.is_error
        assert "ok:create_issue:Hello" in good_call.content

        # Calling device connector via Mcp is rejected
        dev_call = await tools["Mcp"].execute(
            tools["Mcp"].parameters.model_validate(
                {"action": "list", "provider": "obsidian"}
            ),
            _ctx(),
        )
        assert dev_call.is_error
        store.close()

    asyncio.run(run())


def test_http_connector_routes() -> None:
    async def run() -> None:
        store = SQLiteStore(":memory:")
        gateway = EventGateway(store)
        http = HTTPInterface(
            HTTPSettings(
                enabled=True,
                terminals=[
                    HTTPTerminalSettings(
                        terminal_id="http-desktop",
                        user_id="user-1",
                        conversation_id="conv-1",
                        token="desktop-secret-token",
                    )
                ],
            ),
            gateway,
        )
        http.connectors = ConnectorManager(mcp_client=FakeMcpClient())
        app = http.create_app()
        headers = {
            "Authorization": "Bearer desktop-secret-token",
            "X-Terminal-ID": "http-desktop",
        }
        async with TestClient(TestServer(app)) as client:
            # Device sync for Obsidian
            http.terminal_capabilities.update("http-desktop", ["obsidian"])
            resp = await client.post(
                "/v1/connectors/device-sync",
                headers=headers,
                json={
                    "id": "obsidian",
                    "name": "Obsidian",
                    "enabled": True,
                    "config": {"vaultName": "Notes", "noteCount": 12},
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["item"]["id"] == "obsidian"
            assert body["item"]["connected"] is True

            # Add MCP connector with credentials
            resp = await client.post(
                "/v1/connectors/mcp",
                headers=headers,
                json={
                    "name": "GitHub",
                    "endpoint": "https://mcp.github.example/v1",
                    "source": {
                        "kind": "official",
                        "found_at": "https://docs.github.com/mcp",
                        "publisher": "GitHub",
                    },
                    "header_names": ["Authorization"],
                    "credential_label": "Token",
                    "credentials": {"Authorization": "secret-token-123"},
                },
            )
            assert resp.status == 200
            created = await resp.json()
            assert created["item"]["connected"] is True
            assert created["tool_count"] == 2
            assert "secret-token-123" not in json.dumps(created)

            # List connectors
            resp = await client.get("/v1/connectors", headers=headers)
            assert resp.status == 200
            listed = await resp.json()
            assert {item["id"] for item in listed["items"]} == {"obsidian", "github"}

            # Delete MCP connector
            resp = await client.delete("/v1/connectors/github", headers=headers)
            assert resp.status == 200
        store.close()

    asyncio.run(run())
