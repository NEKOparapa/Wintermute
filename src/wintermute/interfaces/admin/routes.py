"""Browser forms and memory views for the Wintermute administrator."""

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web
from jinja2 import Environment, PackageLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from pydantic import ValidationError

from wintermute.config.api_keys import APIKeyUsage
from wintermute.config.models import HTTPAdminSettings, HTTPDeviceBinding
from wintermute.events.core import EventError
from wintermute.events.integrations.devices import DEVICE_FRESH_SECONDS
from wintermute.interfaces.admin import record_data
from wintermute.interfaces.admin.auth import LOGIN, AdminAuth, form_value, redirect
from wintermute.interfaces.http_routes.common import HTTP_INTERFACE, HTTPError
from wintermute.interfaces.http_routes.pairing import PAIRING_MINUTES

_DEVICES = "/admin/devices"
_KEYS = "/admin/keys"
_MEMORY = "/admin/memory"
_RECORDS = "/admin/records"
_RECORD_VIEWS = {
    "chat": "对话历史",
    "conversation": "交流层",
    "notification": "通知层",
    "perception": "感知层",
    "slice": "切片层",
    "record": "记录层",
    "timeline": "合并线",
    "connectors": "连接器",
}
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
        router.add_post(_MEMORY, self.memory)
        router.add_get(_RECORDS, self.records)
        scoped = _RECORDS + "/c-{admin_conversation}"
        for path, handler in (
            ("/history", record_data.history),
            ("/history/screenshot-groups/{group_id}", record_data.history),
            ("/history/compression/sources/{source_id}", record_data.compression_sources),
            ("/history/perceptions/{event_id}/stages", record_data.perception_stages),
            ("/history/traces/{target}", record_data.trace_members),
            ("/history/files/{id}", record_data.attachment),
            ("/items", record_data.list_items),
            ("/items/{item_id}", record_data.get_item_content),
            ("/connectors", record_data.connector_history),
        ):
            router.add_get(scoped + path, handler)
        router.add_static("/admin/static", Path(__file__).parent / "static")

    def _render(
        self,
        request: web.Request,
        page: str,
        *,
        status: int = 200,
        fragment: str | None = None,
        **context: object,
    ) -> web.Response:
        headers: dict[str, str] = {}
        if page != "login":
            context["csrf"] = self.auth.session(request).csrf
            headers["Vary"] = "HX-Request"
            template = (
                f"fragments/{fragment or page}.html"
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

    async def records(self, request: web.Request) -> web.Response:
        scopes = request.app[HTTP_INTERFACE].gateway.store.conversation_scopes()
        conversation = request.query.get("conversation", scopes[0][1] if scopes else "")
        view = request.query.get("view", "chat")
        if view not in _RECORD_VIEWS or any(
            key not in {"conversation", "view"} or len(request.query.getall(key)) != 1
            for key in request.query
        ):
            raise HTTPError(400, "invalid_query")
        if "conversation" in request.query and not any(c == conversation for _, c in scopes):
            raise HTTPError(404, "conversation_not_found")
        return self._render(
            request,
            "records",
            scopes=scopes,
            conversation=conversation,
            views=_RECORD_VIEWS,
            view=view,
        )

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
        store = interface.gateway.store
        error = notice = token = pair_code = ""
        status = 200
        values: dict[str, str] = {}
        issued_device = ""
        history_open = False
        add_step = request.query.get("add") if request.method == "GET" else None
        if add_step not in ("choose", "manual"):
            add_step = None
        if request.method == "POST":
            form = await request.post()
            operation = form_value(form, "operation")
            device_id = form_value(form, "device_id").strip()
            try:
                if operation == "pair" and interface.pairing is not None:
                    pair_code = interface.pairing.create()
                elif operation == "register":
                    add_step = "manual"
                    values = {
                        key: form_value(form, key).strip()
                        for key in ("device_id", "device_type", "display_name")
                    }
                    if not _DEVICE_ID.fullmatch(device_id) or not values["device_type"]:
                        raise ValueError("Invalid device registration")
                    user_id, conversation_id = interface.enrollment_scope()
                    binding = HTTPDeviceBinding.model_validate(
                        {
                            **values,
                            "display_name": values["display_name"] or None,
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                        }
                    )
                    token = interface.devices.register(binding)
                    issued_device = binding.device_id
                    values = {}
                elif operation == "delete":
                    interface.devices.archive(device_id)
                    interface.device_capabilities.forget(device_id)
                    notice = "设备已移至历史设备。"
                    history_open = True
                else:
                    error, status = "不支持的设备操作。", 400
            except EventError:
                error, status = "设备登记冲突，请检查设备 ID。", 409
            except LookupError:
                error, status = "找不到这台设备。", 404
            except (ValidationError, ValueError):
                error, status = "请检查设备 ID、设备类型和显示名称。", 400

        bindings = interface.devices.list()
        profiles = {
            user: store.devices.profiles(user)
            for user in {binding.user_id for binding, _ in bindings}
        }
        groups: dict[str, list[dict[str, object]]] = {"online": [], "offline": [], "archived": []}
        for binding, active in bindings:
            profile = profiles[binding.user_id].get(binding.device_id)
            presence, age = interface.device_capabilities.presence(binding.device_id)
            group = "archived"
            if active:
                group = "online" if presence == "online" else "offline"
            groups[group].append(
                {
                    "binding": binding,
                    "name": (profile.name if profile else None) or binding.display_name,
                    "platform": profile.platform if profile else None,
                    "age": age,
                }
            )
        return self._render(
            request,
            "devices",
            status=status,
            fragment="device_roster"
            if request.method == "GET" and request.query.get("section") == "status"
            else None,
            groups=groups,
            fresh_seconds=DEVICE_FRESH_SECONDS,
            add_step=add_step,
            history_open=history_open,
            values=values,
            error=error,
            notice=notice,
            token=token,
            issued_device=issued_device,
            server_url="https://" + request.host,
            pair_code=pair_code,
            pair_url=f"https://{request.host}/#wm-pair={pair_code}",
            pairing_minutes=PAIRING_MINUTES,
        )

    async def keys(self, request: web.Request) -> web.Response:
        registry = request.app[HTTP_INTERFACE].api_keys
        if registry is None:
            return self._render(request, "keys", status=503, error="密钥服务暂不可用。")
        error = notice = ""
        status = 200
        key_id = request.query.get("key", "")
        if request.method == "POST":
            try:
                form = await request.post()
                key_id = form_value(form, "key_id")
                await asyncio.to_thread(registry.update, key_id, form_value(form, "api_key"))
                notice = "密钥已更新，后续请求将使用新值。"
            except KeyError:
                error, status = "找不到这个模型密钥。", 404
            except ValueError:
                error, status = "密钥格式无效，请检查是否有空格或换行。", 400
            except OSError:
                error, status = "密钥保存失败，请检查服务端存储。", 503
        usages, credentials = await asyncio.to_thread(registry.admin_overview)
        groups: dict[str, list[APIKeyUsage]] = {
            group: [] for group in ("模型调用", "语音与图像", "联网工具")
        }
        affected: dict[str, list[str]] = {}
        for usage in usages:
            groups[usage.group].append(usage)
            affected.setdefault(usage.key_id, []).append(usage.name)
        if key_id and key_id not in affected and not error:
            error, status = "找不到这个模型密钥。", 404
        selected_id = key_id if key_id in affected else usages[0].key_id
        return self._render(
            request,
            "keys",
            status=status,
            groups=groups,
            credentials=credentials,
            affected=affected,
            selected_id=selected_id,
            error=error,
            notice=notice,
        )

    async def memory(self, request: web.Request) -> web.Response:
        interface = request.app[HTTP_INTERFACE]
        users = sorted(interface.memory_vaults)
        if not users:
            if request.method == "POST":
                raise HTTPError(503, "memory_unconfigured")
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
        memory = interface.gateway.store.memory
        schedule_error = notice = ""
        status = 200
        if request.method == "POST":
            form = await request.post()
            try:
                plan = memory.set_schedule(
                    user, int(form_value(form, "interval_days")), form_value(form, "local_time")
                )
            except ValueError:
                plan = memory.schedule(user)
                schedule_error = "间隔天数须为 1 至 3650 天，运行时间请填写有效的时分。"
                status = 400
            else:
                interface.gateway.wakeup()
                notice = "整理计划已保存。"

            if request.headers.get("HX-Request") == "true":
                return self._render(
                    request,
                    "memory",
                    fragment="memory_schedule",
                    status=status,
                    user=user,
                    path=path,
                    plan=plan,
                    schedule_error=schedule_error,
                    notice=notice,
                )
            if not schedule_error:
                url = f"{_MEMORY}?{urlencode({'user': user, 'path': path})}"
                return redirect(request, url)
        else:
            plan = memory.schedule(user)

        error = ""
        preview = None
        areas: dict[str, list[dict[str, object]]] = {area: [] for area in _MEMORY_AREAS}
        try:
            files = await asyncio.to_thread(vault.list_files, max_depth=5)
        except OSError:
            error, status = "记忆目录暂不可读。", 503
        else:
            for item in files:
                area = str(item["path"]).partition("/")[0]
                if not item["is_dir"] and area in areas:
                    areas[area].append(item)

        if path and not error:
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
            plan=plan,
            schedule_error=schedule_error,
            notice=notice,
        )
