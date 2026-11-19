"""Streamable HTTP MCP client (JSON-RPC 2.0) with credential injection and schema checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp

from wintermute.events.storage.repositories.connectors import ConnectorRecord

_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "wintermute", "version": "0.1.0"}


class McpError(Exception):
    """Base error for MCP operations."""


class McpAuthError(McpError):
    """Raised when the remote MCP server rejects credentials (401/403)."""


class McpTransportError(McpError):
    """Raised when the remote MCP endpoint is unreachable or not Streamable HTTP."""


class McpRpcError(McpError):
    """Raised when the remote MCP server returns a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.rpc_message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]


def validate_mcp_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("远程 MCP 地址只支持 https:// 开头的 Streamable HTTP URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("远程 MCP 地址不能包含嵌入凭据或片段标识")
    return value


def _pick_credential(
    credentials: Mapping[str, str],
    *candidates: str | None,
) -> str | None:
    for key in candidates:
        if key and key in credentials and credentials[key]:
            return credentials[key]
    if len(credentials) == 1:
        only_value = next(iter(credentials.values()))
        return only_value or None
    return None


def _build_request_auth(
    endpoint: str,
    connector: ConnectorRecord,
    credentials: Mapping[str, str] | None,
) -> tuple[str, dict[str, str]]:
    headers: dict[str, str] = {}
    url = endpoint
    if not credentials:
        return url, headers

    for header_name in connector.header_names:
        val = _pick_credential(credentials, header_name, connector.credential_label)
        if not val:
            continue
        if header_name.lower() == "authorization" and " " not in val:
            headers[header_name] = f"Bearer {val}"
        else:
            headers[header_name] = val

    if connector.credential_query_param:
        val = _pick_credential(
            credentials, connector.credential_query_param, connector.credential_label
        )
        if val:
            parsed = urlparse(url)
            pairs = [
                (k, v)
                for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k != connector.credential_query_param
            ]
            pairs.append((connector.credential_query_param, val))
            url = urlunparse(parsed._replace(query=urlencode(pairs)))

    if not connector.header_names and not connector.credential_query_param:
        # Fallback if credentials were saved with explicit header-like keys or single token.
        val = _pick_credential(credentials, "Authorization", connector.credential_label)
        if val:
            headers["Authorization"] = val if " " in val else f"Bearer {val}"

    return url, headers


def _parse_sse_jsonrpc(text: str, request_id: int) -> dict[str, Any]:
    last_obj: dict[str, Any] | None = None
    data_lines: list[str] = []

    def flush() -> dict[str, Any] | None:
        nonlocal last_obj
        if not data_lines:
            return None
        payload = "\n".join(data_lines).strip()
        data_lines.clear()
        if not payload or payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict):
            last_obj = obj
            if obj.get("id") == request_id:
                return obj
        return None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            matched = flush()
            if matched is not None:
                return matched
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    matched = flush()
    if matched is not None:
        return matched
    if last_obj is not None:
        return last_obj
    raise McpTransportError("远程 MCP SSE 响应中未包含有效的 JSON-RPC 消息")


def _check_json_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def validate_tool_args(schema: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    if not isinstance(args, Mapping):
        return "args 必须是 JSON 对象"
    required = schema.get("required")
    if isinstance(required, list):
        missing = [k for k in required if isinstance(k, str) and k not in args]
        if missing:
            return f"缺少必填参数: {', '.join(missing)}"
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        if schema.get("additionalProperties") is False:
            extra = [k for k in args if k not in properties]
            if extra:
                return f"包含未定义的参数: {', '.join(extra)}"
        for key, value in args.items():
            prop = properties.get(key)
            if not isinstance(prop, Mapping):
                continue
            expected_type = prop.get("type")
            if isinstance(expected_type, str):
                if not _check_json_type(value, expected_type):
                    return f"参数 {key} 类型应为 {expected_type}"
            elif isinstance(expected_type, list):
                if not any(
                    isinstance(t, str) and _check_json_type(value, t) for t in expected_type
                ):
                    return f"参数 {key} 类型不匹配"
            enum = prop.get("enum")
            if isinstance(enum, list) and value not in enum:
                return f"参数 {key} 必须是允许的枚举值之一"
    return None


class StreamableHttpMcpClient:
    """Stateless-friendly Streamable HTTP MCP client for connector probing and tool calls."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._external_session = session

    async def _post_rpc(
        self,
        session: aiohttp.ClientSession,
        url: str,
        auth_headers: Mapping[str, str],
        session_id: str | None,
        payload: dict[str, Any],
        *,
        expect_response: bool = True,
    ) -> tuple[dict[str, Any] | None, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
            **auth_headers,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                next_session_id = resp.headers.get("Mcp-Session-Id") or session_id
                if resp.status in (401, 403):
                    raise McpAuthError(f"远程 MCP 鉴权失败 (HTTP {resp.status})")
                if resp.status == 405:
                    raise McpTransportError(
                        "该地址不支持 Streamable HTTP POST（可能是旧版 SSE 地址）"
                    )
                if resp.status == 202 or not expect_response:
                    if resp.status >= 400:
                        raise McpTransportError(f"远程 MCP 请求失败 (HTTP {resp.status})")
                    return None, next_session_id
                if resp.status >= 400:
                    raise McpTransportError(f"远程 MCP 返回错误状态 (HTTP {resp.status})")

                content_type = (resp.headers.get("Content-Type") or "").lower()
                text = await resp.text()
                if "text/event-stream" in content_type:
                    req_id = int(payload.get("id", 0))
                    data = _parse_sse_jsonrpc(text, req_id)
                else:
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise McpTransportError(
                            "远程 MCP 未返回有效的 JSON-RPC 响应，请确认地址为 Streamable HTTP"
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise McpTransportError("远程 MCP 响应格式无效")
                    data = parsed
                if "error" in data and data["error"] is not None:
                    err = data["error"]
                    code = int(err.get("code", -32000)) if isinstance(err, dict) else -32000
                    msg = (
                        str(err.get("message", "MCP error"))
                        if isinstance(err, dict)
                        else str(err)
                    )
                    err_data = err.get("data") if isinstance(err, dict) else None
                    raise McpRpcError(code, msg, err_data)
                return data, next_session_id
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise McpTransportError(f"无法连接远程 MCP 服务器: {type(exc).__name__}") from exc

    async def _initialize(
        self,
        session: aiohttp.ClientSession,
        url: str,
        auth_headers: Mapping[str, str],
    ) -> str | None:
        _, session_id = await self._post_rpc(
            session,
            url,
            auth_headers,
            None,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            },
        )
        # Send initialized notification; ignore non-fatal errors on stateless servers.
        try:
            await self._post_rpc(
                session,
                url,
                auth_headers,
                session_id,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                expect_response=False,
            )
        except McpError:
            pass
        return session_id

    async def list_tools(
        self,
        connector: ConnectorRecord,
        credentials: Mapping[str, str] | None = None,
    ) -> list[McpToolInfo]:
        if not connector.endpoint:
            raise McpTransportError("该连接器未配置 MCP 地址")
        url, auth_headers = _build_request_auth(connector.endpoint, connector, credentials)

        async def _run(session: aiohttp.ClientSession) -> list[McpToolInfo]:
            session_id = await self._initialize(session, url, auth_headers)
            tools: list[McpToolInfo] = []
            cursor: str | None = None
            req_id = 2
            for _ in range(10):
                params: dict[str, Any] = {}
                if cursor:
                    params["cursor"] = cursor
                resp, session_id = await self._post_rpc(
                    session,
                    url,
                    auth_headers,
                    session_id,
                    {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params},
                )
                req_id += 1
                result = (resp or {}).get("result")
                if not isinstance(result, dict):
                    break
                raw_tools = result.get("tools")
                if isinstance(raw_tools, list):
                    for item in raw_tools:
                        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                            continue
                        schema = item.get("inputSchema")
                        input_schema = (
                            schema if isinstance(schema, dict) else {"type": "object"}
                        )
                        tools.append(
                            McpToolInfo(
                                name=item["name"],
                                description=str(item.get("description") or ""),
                                input_schema=input_schema,
                            )
                        )
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
            return tools

        if self._external_session is not None:
            return await _run(self._external_session)
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            return await _run(session)

    async def call_tool(
        self,
        connector: ConnectorRecord,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not connector.endpoint:
            raise McpTransportError("该连接器未配置 MCP 地址")
        url, auth_headers = _build_request_auth(connector.endpoint, connector, credentials)

        async def _run(session: aiohttp.ClientSession) -> dict[str, Any]:
            session_id = await self._initialize(session, url, auth_headers)
            resp, _ = await self._post_rpc(
                session,
                url,
                auth_headers,
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": dict(arguments)},
                },
            )
            result = (resp or {}).get("result")
            if not isinstance(result, dict):
                text = json.dumps(result, ensure_ascii=False)
                return {"content": [{"type": "text", "text": text}]}
            return result

        if self._external_session is not None:
            return await _run(self._external_session)
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            return await _run(session)
