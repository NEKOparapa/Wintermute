"""Browser forms and memory views for the Purin administrator."""

import asyncio
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from zoneinfo import available_timezones

import segno
from aiohttp import web
from jinja2 import Environment, PackageLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from purin.config.api_keys import APIKeyUsage
from purin.config.models import HTTPAdminSettings
from purin.events.integrations.devices import DEVICE_FRESH_SECONDS
from purin.interfaces.admin import record_data
from purin.interfaces.admin.auth import LOGIN, AdminAuth, form_value, redirect
from purin.interfaces.http_routes.common import HTTP_INTERFACE, HTTPError
from purin.interfaces.http_routes.pairing import PAIRING_MINUTES
from purin.memory.frontmatter import split_frontmatter

_DEVICES = "/admin/devices"
_KEYS = "/admin/keys"
_MEMORY = "/admin/memory"
_RECORDS = "/admin/records"
_SETTINGS = "/admin/settings"
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
_MEMORY_AREAS = {
    "agent-character": "角色",
    "about-user": "用户档案",
    "note": "笔记",
    "log": "日志",
}
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")


@cache
def _timezones() -> tuple[str, ...]:
    return tuple(sorted(available_timezones()))


def _memory_preview(path: str, content: str) -> tuple[str | None, Markup]:
    header = None
    if path.startswith("note/"):
        try:
            header, content = split_frontmatter(content)
        except ValueError:
            # Keep a note with an unclosed metadata block readable.
            pass
    return header, Markup(_MARKDOWN.render(content))


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
            loader=PackageLoader("purin.interfaces.admin"),
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
        router.add_get(_SETTINGS, self.settings)
        router.add_post(_SETTINGS, self.settings)
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
        error = notice = pair_url = ""
        status = 200
        history_open = False
        if request.method == "POST":
            form = await request.post()
            operation = form_value(form, "operation")
            if operation == "pair" and interface.pairing is not None:
                pair_url = f"https://{request.host}/#purin-pair={interface.pairing.create()}"
            elif operation == "delete":
                device_id = form_value(form, "device_id").strip()
                try:
                    interface.devices.archive(device_id)
                    interface.device_capabilities.forget(device_id)
                    notice = "设备已移至历史设备。"
                    history_open = True
                except LookupError:
                    error, status = "找不到这台设备。", 404
            else:
                error, status = "不支持的设备操作。", 400

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
            history_open=history_open,
            error=error,
            notice=notice,
            pair_url=pair_url,
            pair_qr=segno.make_qr(pair_url, error="M").png_data_uri(scale=6) if pair_url else "",
            pairing_minutes=PAIRING_MINUTES,
        )

    async def keys(self, request: web.Request) -> web.Response:
        registry = request.app[HTTP_INTERFACE].api_keys
        if registry is None:
            return self._render(request, "keys", status=503, error="密钥服务暂不可用。")
        usages, credentials = await asyncio.to_thread(registry.admin_overview)
        calls_by_key: dict[str, list[APIKeyUsage]] = {}
        for usage in usages:
            calls_by_key.setdefault(usage.key_id, []).append(usage)

        error = notice = ""
        status = 200
        form = await request.post() if request.method == "POST" else None
        key_id = form_value(form, "key_id") if form is not None else request.query.get("key")
        if key_id is not None and key_id not in calls_by_key:
            error, status = "找不到这个模型密钥。", 404
        elif form is not None and key_id is not None:
            try:
                await asyncio.to_thread(registry.update, key_id, form_value(form, "api_key"))
            except ValueError:
                error, status = "密钥格式无效，请检查是否有空格或换行。", 400
            except OSError:
                error, status = "密钥保存失败，请检查服务端存储。", 503
            else:
                _, credentials = await asyncio.to_thread(registry.admin_overview)
                notice = "密钥已更新，后续请求将使用新值。"
        return self._render(
            request,
            "keys",
            status=status,
            calls_by_key=calls_by_key,
            credentials=credentials,
            selected_id=key_id if key_id in calls_by_key else None,
            error=error,
            notice=notice,
        )

    async def memory(self, request: web.Request) -> web.Response:
        interface = request.app[HTTP_INTERFACE]
        if any(key != "path" or len(request.query.getall(key)) != 1 for key in request.query):
            raise HTTPError(400, "invalid_query")
        vault = interface.memory_vaults.get(interface.memory_user_id)
        if vault is None:
            return self._render(request, "memory", areas=None)
        path = request.query.get("path", "")

        status = 200
        error = ""
        preview = None
        frontmatter = None
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

        selected = next(
            (
                item
                for files in areas.values()
                for item in files
                if not path or item["path"] == path
            ),
            None,
        )
        if selected is not None:
            path = str(selected["path"])
        if path and not error:
            try:
                content = await asyncio.to_thread(vault.read_text, path)
                frontmatter, preview = await asyncio.to_thread(_memory_preview, path, content)
            except (FileNotFoundError, PermissionError, ValueError):
                error, status = "找不到此记忆文件，或该文件无法读取。", 404
            except OSError:
                error, status = "读取记忆时发生错误。", 503
        return self._render(
            request,
            "memory",
            status=status,
            path=path,
            selected=selected,
            areas=areas,
            area_labels=_MEMORY_AREAS,
            preview=preview,
            frontmatter=frontmatter,
            error=error,
        )

    async def settings(self, request: web.Request) -> web.Response:
        interface = request.app[HTTP_INTERFACE]
        if request.query:
            raise HTTPError(400, "invalid_query")
        if not interface.memory_vaults:
            return self._render(
                request, "settings", plan=None, status=503 if request.method == "POST" else 200
            )

        gateway = interface.gateway
        memory = gateway.store.memory
        zones = await asyncio.to_thread(_timezones)
        if request.method == "POST":
            form = await request.post()
            fields = set(form) - {"csrf"}
            if not fields or not fields <= {"interval_days", "local_time", "timezone"}:
                return web.Response(status=400, text="无法识别修改的设置。")
            timezone = form_value(form, "timezone") if "timezone" in fields else None
            if timezone is not None and timezone not in zones:
                return web.Response(status=400, text="请选择有效的时间地区。")
            try:
                memory.set_schedule(
                    interface.memory_user_id,
                    interval_days=int(form_value(form, "interval_days"))
                    if "interval_days" in fields
                    else None,
                    local_time=form_value(form, "local_time") if "local_time" in fields else None,
                    timezone=timezone,
                )
            except ValueError:
                return web.Response(
                    status=400, text="整理间隔须为 1 至 3650 天，触发时间须为有效的时分。"
                )
            gateway.wakeup()
            if (
                request.headers.get("Accept") == "text/plain"
                or request.headers.get("HX-Request") == "true"
            ):
                return web.Response(status=204)
            return redirect(request, _SETTINGS)
        return self._render(
            request,
            "settings",
            plan=memory.schedule(interface.memory_user_id),
            zones=zones,
        )
