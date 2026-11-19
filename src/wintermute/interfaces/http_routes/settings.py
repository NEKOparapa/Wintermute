"""用户档案和模型密钥设置。"""

import asyncio
from typing import Literal, TypedDict

from aiohttp import web
from pydantic import Field

from .common import (
    HTTP_INTERFACE,
    ClientId,
    HTTPError,
    HTTPInput,
    key_transport_allowed,
    read_input,
)

routes = web.RouteTableDef()


class ProfileDocument(TypedDict):
    status: Literal["unconfigured", "empty", "ready"]
    markdown: str
    effective: bool


class _APIKeyInput(HTTPInput):
    id: ClientId
    api_key: str = Field(repr=False)


@routes.get("/v1/profile")
async def profile(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if request.query:
        raise HTTPError(400, "invalid_query")
    vault = interface.memory_vaults.get(binding.user_id)
    document: ProfileDocument = {"status": "unconfigured", "markdown": "", "effective": False}
    if vault is not None:
        try:
            markdown = await asyncio.to_thread(vault.read_text, "about-user/profile.md")
        except FileNotFoundError:
            markdown = ""
        except (OSError, ValueError):
            raise HTTPError(503, "profile_unavailable") from None
        document = {
            "status": "ready" if markdown.strip() else "empty",
            "markdown": markdown,
            "effective": bool(markdown.strip()),
        }
    return web.json_response(document, headers={"Cache-Control": "private, no-store"})


@routes.post("/v1/settings/api-keys")
@routes.get("/v1/settings/api-keys")
async def manage_api_keys(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if not binding.manage_api_keys:
        raise HTTPError(403, "api_key_management_forbidden")
    if not key_transport_allowed(request):
        raise HTTPError(403, "secure_transport_required")
    if request.query:
        raise HTTPError(400, "invalid_query")
    if interface.api_keys is None:
        raise HTTPError(503, "api_key_management_unavailable")
    try:
        if request.method == "POST":
            data = await read_input(request, _APIKeyInput, "invalid_request")
            items = await asyncio.to_thread(interface.api_keys.update, data.id, data.api_key)
        else:
            items = await asyncio.to_thread(interface.api_keys.list_items)
    except KeyError:
        raise HTTPError(404, "unknown_key_id") from None
    except ValueError:
        raise HTTPError(400, "invalid_api_key") from None
    except OSError:
        raise HTTPError(503, "api_key_management_unavailable") from None
    return web.json_response({"items": items})
