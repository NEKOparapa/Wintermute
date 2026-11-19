"""设备路由共用的应用状态、请求校验和错误类型。"""

import ipaddress
import json
from typing import TYPE_CHECKING, Annotated, Any

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from wintermute.interfaces.http import HTTPInterface

HTTP_INTERFACE: web.AppKey["HTTPInterface"] = web.AppKey("wintermute.http")

type ClientId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
type NonblankText = Annotated[str, Field(min_length=1, pattern=r"\S")]


class HTTPInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class HTTPError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def key_transport_allowed(request: web.Request) -> bool:
    if request.secure:
        return True
    # 仅信任同机代理覆盖的协议头；外部直连不能用转发头伪装成 TLS。
    if not _is_loopback(request.remote):
        return False
    protocols = request.headers.getall("X-Forwarded-Proto", [])
    if protocols:
        return protocols == ["https"]
    if any(name in request.headers for name in ("Forwarded", "X-Forwarded-For", "Via")):
        return False
    return _is_loopback(request.url.host)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("JSON must contain finite numbers")


async def read_input[T: HTTPInput](request: web.Request, schema: type[T], error_code: str) -> T:
    if request.content_type != "application/json":
        raise HTTPError(415, "json_required")
    try:
        body = json.loads(
            await request.text(), object_pairs_hook=_json_pairs, parse_constant=_reject_constant
        )
        return schema.model_validate(body)
    except (ValueError, RecursionError):
        raise HTTPError(400, error_code) from None
