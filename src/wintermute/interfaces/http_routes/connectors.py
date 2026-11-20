"""连接器管理与设备连接器同步路由。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from aiohttp import web
from pydantic import Field, JsonValue

from wintermute.events.integrations.connectors import is_connector_connected
from wintermute.events.integrations.mcp_client import (
    McpAuthError,
    McpError,
    validate_mcp_endpoint,
)
from wintermute.events.storage.repositories.connectors import ConnectorRecord, ConnectorSource

from .common import HTTP_INTERFACE, HTTPError, HTTPInput, key_transport_allowed, read_input

if TYPE_CHECKING:
    from wintermute.config.models import HTTPDeviceBinding
    from wintermute.interfaces.http import HTTPInterface

routes = web.RouteTableDef()


class _SourceInput(HTTPInput):
    kind: Literal["official", "third_party"] = "third_party"
    found_at: str = Field(min_length=1, max_length=2048)
    publisher: str = Field(min_length=1, max_length=256)


class _McpConnectorInput(HTTPInput):
    name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2048)
    source: _SourceInput
    header_names: list[str] = Field(default_factory=list, max_length=8)
    credential_label: str | None = Field(default=None, max_length=64)
    credential_query_param: str | None = Field(default=None, max_length=128)
    credentials: dict[str, str] | None = Field(default=None, repr=False)
    connection_id: str = Field(default="default", min_length=1, max_length=64)
    connection_label: str = Field(default="默认账号", min_length=1, max_length=64)


class _CredentialsInput(HTTPInput):
    values: dict[str, str] = Field(min_length=1, repr=False)
    connection_id: str = Field(default="default", min_length=1, max_length=64)
    connection_label: str = Field(default="默认账号", min_length=1, max_length=64)


class _DeviceSyncInput(HTTPInput):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    enabled: bool
    config: dict[str, JsonValue] = Field(default_factory=dict)


def _serialize_connector(
    record: ConnectorRecord,
    connected: bool,
    status: str,
) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "tool_type": record.tool_type,
        "connected": connected,
        "status": status,
        "endpoint": record.endpoint,
        "source": record.source.model_dump(mode="json") if record.source else None,
        "header_names": record.header_names,
        "credential_label": record.credential_label,
        "credential_query_param": record.credential_query_param,
        "auth_mode": record.auth_mode,
        "device_id": record.device_id,
        "config": record.config,
        "verified": record.verified,
        "enabled": record.enabled,
        "has_credentials": record.has_credentials,
        "connections": [c.model_dump(mode="json") for c in record.connections],
    }


def _bound_map(interface: HTTPInterface, user_id: str, conversation_id: str) -> dict[str, str]:
    return {
        t.device_id: t.display_name or t.device_id
        for t in interface.devices.active()
        if t.user_id == user_id and t.conversation_id == conversation_id
    }


def _selected_connector(request: web.Request, binding: HTTPDeviceBinding) -> ConnectorRecord:
    interface = request.app[HTTP_INTERFACE]
    query = request.query
    if set(query) - {"device_id"} or len(query.getall("device_id", [])) > 1:
        raise HTTPError(400, "invalid_query")
    connector_id = request.match_info["id"]
    store = interface.gateway.store.connectors
    if "device_id" not in query:
        try:
            return store.get_account(binding.user_id, connector_id)
        except LookupError:
            pass
    device_id = query.get("device_id", binding.device_id)
    if device_id in _bound_map(interface, binding.user_id, binding.conversation_id):
        devices = store.list_devices(binding.user_id, (device_id,), connector_id)
        if devices:
            return devices[0]
    raise HTTPError(404, "connector_not_found")


@routes.get("/v1/connectors")
async def list_connectors(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    bound = _bound_map(interface, binding.user_id, binding.conversation_id)
    store = interface.gateway.store.connectors
    records = store.list(binding.user_id, device_ids=bound)
    items = []
    for rec in records:
        connected, status = is_connector_connected(rec, interface.device_capabilities, bound)
        items.append(_serialize_connector(rec, connected, status))
    return web.json_response({"items": items})


@routes.get("/v1/connectors/history")
async def connector_history(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    query = request.query
    if set(query) - {"cursor", "limit"} or any(len(query.getall(k)) != 1 for k in query):
        raise HTTPError(400, "invalid_query")
    try:
        items, cursor = interface.gateway.store.connectors.list_history(
            user_id=binding.user_id,
            conversation_id=binding.conversation_id,
            limit=int(query.get("limit", "30")),
            cursor=query.get("cursor"),
        )
    except ValueError:
        raise HTTPError(400, "invalid_query") from None
    return web.json_response(
        {"items": [item.model_dump(mode="json") for item in items], "next_cursor": cursor}
    )


@routes.post("/v1/connectors/mcp")
async def upsert_mcp_connector(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    data = await read_input(request, _McpConnectorInput, "invalid_connector_request")
    if data.credentials and not key_transport_allowed(request):
        raise HTTPError(403, "secure_transport_required")
    try:
        endpoint = validate_mcp_endpoint(data.endpoint)
    except ValueError:
        raise HTTPError(400, "invalid_mcp_endpoint") from None

    store = interface.gateway.store
    source = ConnectorSource(
        kind=data.source.kind,
        found_at=data.source.found_at,
        publisher=data.source.publisher,
    )
    try:
        rec = store.connectors.upsert_mcp(
            binding.user_id,
            name=data.name,
            endpoint=endpoint,
            source=source,
            header_names=data.header_names,
            credential_label=data.credential_label,
            credential_query_param=data.credential_query_param,
            verified=False,
        )
        if data.credentials:
            rec = store.connectors.save_credentials(
                binding.user_id,
                rec.id,
                data.credentials,
                connection_id=data.connection_id,
                label=data.connection_label,
                verified=False,
            )
    except ValueError:
        raise HTTPError(400, "invalid_connector_request") from None

    interface.connectors.invalidate(binding.user_id, rec.id)
    verify_error: str | None = None
    tool_count: int | None = None
    should_probe = rec.auth_mode == "none" or rec.has_credentials
    if should_probe:
        try:
            tools = await interface.connectors.get_tools(
                store, binding.user_id, rec, data.connection_id, refresh=True
            )
            tool_count = len(tools)
            rec = store.connectors.set_verified(
                binding.user_id,
                rec.id,
                True,
                connection_id=data.connection_id if data.credentials else None,
            )
        except McpAuthError as exc:
            verify_error = str(exc)
        except McpError as exc:
            verify_error = str(exc)

    bound = _bound_map(interface, binding.user_id, binding.conversation_id)
    connected, status = is_connector_connected(rec, interface.device_capabilities, bound)
    return web.json_response(
        {
            "item": _serialize_connector(rec, connected, status),
            "tool_count": tool_count,
            "verify_error": verify_error,
        }
    )


@routes.post("/v1/connectors/{id}/credentials")
async def save_connector_credentials(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if not key_transport_allowed(request):
        raise HTTPError(403, "secure_transport_required")
    if request.query:
        raise HTTPError(400, "invalid_query")
    connector_id = request.match_info["id"]
    data = await read_input(request, _CredentialsInput, "invalid_credentials")
    store = interface.gateway.store
    try:
        rec = store.connectors.save_credentials(
            binding.user_id,
            connector_id,
            data.values,
            connection_id=data.connection_id,
            label=data.connection_label,
            verified=False,
        )
    except LookupError:
        raise HTTPError(404, "connector_not_found") from None
    except ValueError:
        raise HTTPError(400, "invalid_credentials") from None

    interface.connectors.invalidate(binding.user_id, rec.id)
    verify_error: str | None = None
    tool_count: int | None = None
    if rec.tool_type == "mcp":
        try:
            tools = await interface.connectors.get_tools(
                store, binding.user_id, rec, data.connection_id, refresh=True
            )
            tool_count = len(tools)
            rec = store.connectors.set_verified(
                binding.user_id, rec.id, True, connection_id=data.connection_id
            )
        except McpError as exc:
            verify_error = str(exc)
    else:
        rec = store.connectors.set_verified(
            binding.user_id, rec.id, True, connection_id=data.connection_id
        )

    bound = _bound_map(interface, binding.user_id, binding.conversation_id)
    connected, status = is_connector_connected(rec, interface.device_capabilities, bound)
    return web.json_response(
        {
            "item": _serialize_connector(rec, connected, status),
            "tool_count": tool_count,
            "verify_error": verify_error,
        }
    )


@routes.post("/v1/connectors/{id}/test")
async def test_connector(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    store = interface.gateway.store
    rec = _selected_connector(request, binding)

    if rec.tool_type != "mcp":
        bound = _bound_map(interface, binding.user_id, binding.conversation_id)
        connected, status = is_connector_connected(rec, interface.device_capabilities, bound)
        return web.json_response(
            {
                "item": _serialize_connector(rec, connected, status),
                "tool_count": None,
                "verify_error": None if connected else "设备连接器当前离线",
            }
        )

    verify_error: str | None = None
    tool_count: int | None = None
    try:
        tools = await interface.connectors.get_tools(store, binding.user_id, rec, refresh=True)
        tool_count = len(tools)
        rec = store.connectors.set_verified(binding.user_id, rec.id, True)
    except McpError as exc:
        verify_error = str(exc)
        rec = store.connectors.set_verified(binding.user_id, rec.id, False)

    bound = _bound_map(interface, binding.user_id, binding.conversation_id)
    connected, status = is_connector_connected(rec, interface.device_capabilities, bound)
    return web.json_response(
        {
            "item": _serialize_connector(rec, connected, status),
            "tool_count": tool_count,
            "verify_error": verify_error,
        }
    )


@routes.delete("/v1/connectors/{id}")
async def delete_connector(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    rec = _selected_connector(request, binding)
    try:
        interface.gateway.store.connectors.delete(binding.user_id, rec.id, device_id=rec.device_id)
    except LookupError:
        raise HTTPError(404, "connector_not_found") from None
    if rec.tool_type == "mcp":
        interface.connectors.invalidate(binding.user_id, rec.id)
    return web.json_response({"deleted": True, "id": rec.id})


@routes.post("/v1/connectors/device-sync")
async def sync_device_connector(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate_tool(request)
    data = await read_input(request, _DeviceSyncInput, "invalid_device_connector")
    try:
        rec = interface.gateway.store.connectors.upsert_device(
            binding.user_id,
            connector_id=data.id,
            name=data.name,
            device_id=binding.device_id,
            enabled=data.enabled,
            config=data.config,
        )
    except ValueError:
        raise HTTPError(400, "invalid_device_connector") from None
    bound = _bound_map(interface, binding.user_id, binding.conversation_id)
    connected, status = is_connector_connected(rec, interface.device_capabilities, bound)
    return web.json_response({"item": _serialize_connector(rec, connected, status)})
