"""Browser forms and read-only memory views for the Wintermute administrator."""

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path

from aiohttp import web
from jinja2 import Environment, PackageLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from pydantic import ValidationError

from wintermute.config.models import HTTPAdminSettings, HTTPDeviceBinding
from wintermute.events.core import EventError
from wintermute.interfaces.admin.auth import LOGIN, AdminAuth, form_value, redirect
from wintermute.interfaces.http_routes.common import HTTP_INTERFACE, HTTPError

_DEVICES = "/admin/devices"
_KEYS = "/admin/keys"
_MEMORY = "/admin/memory"
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_MEMORY_AREAS = {
    "agent-character": "角色",
    "about-user": "用户档案",
    "note": "笔记",
    "log": "日志",
}
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")


def _elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


class AdminPanel:
    def __init__(self, settings: HTTPAdminSettings, environ: Mapping[str, str] | None) -> None:
        self.auth = AdminAuth(settings, environ)
        self.templates = Environment(
            loader=PackageLoader("wintermute.interfaces.admin"),
            autoescape=select_autoescape(),
        )
        self.templates.filters["elapsed"] = _elapsed

    def add_routes(self, app: web.Application) -> None:
        router = app.router
        router.add_get("/admin", self.home)
        router.add_get("/admin/", self.home)
        router.add_get(LOGIN, self.login)
        router.add_post(LOGIN, self.login)
        router.add_post("/admin/logout", self.logout)
        router.add_get(_DEVICES, self.devices)
        router.add_post(_DEVICES, self.devices)
        router.add_get(_KEYS, self.keys)
        router.add_post(_KEYS, self.keys)
        router.add_get(_MEMORY, self.memory)
        router.add_static("/admin/static", Path(__file__).parent / "static")

    def _render(
        self, request: web.Request, page: str, *, status: int = 200, **context: object
    ) -> web.Response:
        headers: dict[str, str] = {}
        if page != "login":
            context["csrf"] = self.auth.session(request).csrf
            headers["Vary"] = "HX-Request"
            template = (
                f"fragments/{page}.html"
                if request.headers.get("HX-Request") == "true"
                else f"{page}.html"
            )
        else:
            template = "login.html"
        return web.Response(
            text=self.templates.get_template(template).render(page=page, **context),
            content_type="text/html",
            status=status,
            headers=headers,
        )

    async def home(self, request: web.Request) -> web.Response:
        return redirect(request, _DEVICES)

    async def login(self, request: web.Request) -> web.Response:
        if self.auth.signed_in(request):
            return redirect(request, _DEVICES)
        if request.method != "POST":
            return self._render(request, "login")
        valid = await self.auth.check_password(form_value(await request.post(), "password"))
        if valid is None:
            response = self._render(
                request, "login", status=429, error="尝试次数过多，请稍后再试。"
            )
            response.headers["Retry-After"] = "60"
            return response
        if not valid:
            return self._render(request, "login", status=401, error="管理员口令错误。")
        response = redirect(request, _DEVICES)
        self.auth.start(request, response)
        return response

    async def logout(self, request: web.Request) -> web.Response:
        response = redirect(request, LOGIN)
        self.auth.end(request, response)
        return response

    async def devices(self, request: web.Request) -> web.Response:
        interface = request.app[HTTP_INTERFACE]
        error = notice = token = ""
        status = 200
        values: dict[str, str] = {}
        issued_device = ""
        if request.method == "POST":
            form = await request.post()
            operation = form_value(form, "operation")
            device_id = form_value(form, "device_id").strip()
            try:
                if operation == "register":
                    values = {
                        key: form_value(form, key).strip()
                        for key in (
                            "device_id",
                            "device_type",
                            "user_id",
                            "conversation_id",
                            "display_name",
                        )
                    }
                    if not _DEVICE_ID.fullmatch(device_id) or not values["device_type"]:
                        raise ValueError("Invalid device registration")
                    binding = HTTPDeviceBinding.model_validate(
                        {**values, "display_name": values["display_name"] or None}
                    )
                    token = interface.devices.register(binding)
                    issued_device = binding.device_id
                    notice = "设备已登记。"
                    values = {}
                elif operation in {"rotate", "revoke"}:
                    token = (
                        interface.devices.change_credential(device_id, revoke=operation == "revoke")
                        or ""
                    )
                    interface.device_capabilities.forget(device_id)
                    issued_device = device_id
                    notice = "已签发新令牌。" if token else "设备已撤销。"
                else:
                    raise ValueError("Invalid device operation")
            except EventError:
                error, status = "设备 ID 已被使用，或该会话属于其他用户。", 409
            except LookupError:
                error, status = "找不到这台设备。", 404
            except (ValidationError, ValueError):
                error, status = "请检查设备 ID、设备类型、用户和会话。", 400

        bindings = interface.devices.list()
        profiles = {
            user: interface.gateway.store.devices.profiles(user)
            for user in {binding.user_id for binding, _ in bindings}
        }
        devices = []
        for binding, active in bindings:
            profile = profiles[binding.user_id].get(binding.device_id)
            presence, age = interface.device_capabilities.presence(binding.device_id)
            devices.append(
                {
                    "binding": binding,
                    "name": (profile.name if profile else None) or binding.display_name,
                    "platform": profile.platform if profile else None,
                    "active": active,
                    "presence": presence,
                    "age": age,
                }
            )
        return self._render(
            request,
            "devices",
            status=status,
            devices=devices,
            values=values,
            error=error,
            notice=notice,
            token=token,
            issued_device=issued_device,
            server_url="https://" + request.host,
            users=sorted(profiles.keys() | interface.memory_vaults.keys()),
            conversations=sorted({binding.conversation_id for binding, _ in bindings}),
        )

    async def keys(self, request: web.Request) -> web.Response:
        registry = request.app[HTTP_INTERFACE].api_keys
        if registry is None:
            return self._render(request, "keys", status=503, error="密钥服务暂不可用。")
        error = notice = ""
        status = 200
        if request.method == "POST":
            try:
                form = await request.post()
                key_id = form_value(form, "key_id")
                items = await asyncio.to_thread(
                    registry.update, key_id, form_value(form, "api_key")
                )
                notice = "密钥已更新，后续请求将使用新值。"
            except KeyError:
                error, status = "找不到这个模型密钥。", 404
            except ValueError:
                error, status = "密钥格式无效，请检查是否有空格或换行。", 400
            except OSError:
                error, status = "密钥保存失败，请检查服务端存储。", 503
            else:
                return self._render(request, "keys", items=items, notice=notice)
        items = await asyncio.to_thread(registry.list_items)
        return self._render(request, "keys", status=status, items=items, error=error)

    async def memory(self, request: web.Request) -> web.Response:
        interface = request.app[HTTP_INTERFACE]
        users = sorted(interface.memory_vaults)
        if not users:
            return self._render(request, "memory", users=users)
        if any(
            key not in {"user", "path"} or len(request.query.getall(key)) != 1
            for key in request.query
        ):
            raise HTTPError(400, "invalid_query")
        user = request.query.get("user", users[0])
        vault = interface.memory_vaults.get(user)
        if vault is None:
            raise HTTPError(404, "memory_user_not_found")
        path = request.query.get("path", "")
        plan = interface.gateway.store.memory.schedule(user)
        try:
            files = await asyncio.to_thread(vault.list_files, max_depth=5)
        except OSError:
            return self._render(
                request, "memory", status=503, users=users, user=user, error="记忆目录暂不可读。"
            )

        areas: dict[str, list[dict[str, object]]] = {area: [] for area in _MEMORY_AREAS}
        for item in files:
            area = str(item["path"]).partition("/")[0]
            if not item["is_dir"] and area in areas:
                areas[area].append(item)

        preview = None
        error = ""
        status = 200
        if path:
            try:
                content = await asyncio.to_thread(vault.read_text, path)
                preview = Markup(await asyncio.to_thread(_MARKDOWN.render, content))
            except (FileNotFoundError, PermissionError, ValueError):
                error, status = "找不到此记忆文件，或该文件无法读取。", 404
            except OSError:
                error, status = "读取记忆时发生错误。", 503
        return self._render(
            request,
            "memory",
            status=status,
            users=users,
            user=user,
            path=path,
            areas=areas,
            area_labels=_MEMORY_AREAS,
            preview=preview,
            error=error,
            next_run=plan.next_run_at.strftime("%Y-%m-%d %H:%M"),
            timezone=plan.zone.key,
        )
