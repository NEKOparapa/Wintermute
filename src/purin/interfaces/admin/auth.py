"""HTTPS-only administrator sessions for a single Purin process."""

import asyncio
import os
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from hmac import compare_digest
from secrets import token_bytes, token_urlsafe
from time import monotonic

from aiohttp import web
from multidict import MultiDictProxy

from purin.config.models import ConfigError, HTTPAdminSettings
from purin.interfaces.http_routes.common import HTTPError, trusted_https
from purin.logging.setup import protect_secrets

LOGIN = "/admin/login"
_COOKIE = "__Host-purin-admin"
_SESSION_TTL = 8 * 3600
_LOGIN_WINDOW = 60
_LOGIN_LIMIT = 12


@dataclass(frozen=True)
class AdminSession:
    csrf: str
    expires: float


def is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def redirect(request: web.Request, location: str) -> web.Response:
    if request.headers.get("HX-Request") == "true":
        return web.Response(headers={"HX-Redirect": location})
    return web.Response(status=303, headers={"Location": location})


def form_value[T](form: MultiDictProxy[T], key: str) -> str:
    values = form.getall(key, [])
    return values[0] if len(values) == 1 and isinstance(values[0], str) else ""


class AdminAuth:
    def __init__(self, settings: HTTPAdminSettings, environ: Mapping[str, str] | None) -> None:
        values = os.environ if environ is None else environ
        password = values.get(settings.password_env, "")
        if not 16 <= len(password) <= 1024 or not password.strip():
            raise ConfigError(f"Set {settings.password_env} to a 16–1024 character password")
        protect_secrets((password,))
        self._salt = token_bytes(16)
        self._password_hash = self._hash(password)
        self._sessions: dict[str, AdminSession] = {}
        self._attempts: deque[float] = deque()

    def _hash(self, password: str) -> bytes:
        return pbkdf2_hmac("sha256", password.encode("utf-8"), self._salt, 600_000)

    def _get(self, request: web.Request) -> AdminSession | None:
        token = request.cookies.get(_COOKIE, "")
        session = self._sessions.get(token)
        if session is not None and session.expires <= monotonic():
            self._sessions.pop(token, None)
            return None
        return session

    def signed_in(self, request: web.Request) -> bool:
        return self._get(request) is not None

    def session(self, request: web.Request) -> AdminSession:
        session = self._get(request)
        if session is None:
            raise HTTPError(401, "admin_session_expired")
        return session

    async def check_password(self, password: str) -> bool | None:
        now = monotonic()
        while self._attempts and self._attempts[0] <= now - _LOGIN_WINDOW:
            self._attempts.popleft()
        if len(self._attempts) >= _LOGIN_LIMIT:
            return None
        self._attempts.append(now)
        if not 16 <= len(password) <= 1024:
            return False
        return compare_digest(await asyncio.to_thread(self._hash, password), self._password_hash)

    def start(self, request: web.Request, response: web.Response) -> None:
        self._sessions.pop(request.cookies.get(_COOKIE, ""), None)
        now = monotonic()
        self._sessions = {key: item for key, item in self._sessions.items() if item.expires > now}
        if len(self._sessions) >= 128:
            self._sessions.pop(next(iter(self._sessions)))
        token = token_urlsafe(32)
        self._sessions[token] = AdminSession(token_urlsafe(32), now + _SESSION_TTL)
        response.set_cookie(_COOKIE, token, secure=True, httponly=True, samesite="Strict", path="/")

    def end(self, request: web.Request, response: web.Response) -> None:
        self._sessions.pop(request.cookies.get(_COOKIE, ""), None)
        response.set_cookie(
            _COOKIE, "", max_age=0, secure=True, httponly=True, samesite="Strict", path="/"
        )

    @web.middleware
    async def headers(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        response = await handler(request)
        if is_admin_path(request.path):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @web.middleware
    async def authorize(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        path = request.path
        if not is_admin_path(path):
            return await handler(request)
        if not trusted_https(request):
            return web.Response(status=403, text="管理面板需要 HTTPS。")
        if path.startswith("/admin/static/") and request.method in {"GET", "HEAD"}:
            return await handler(request)

        login = path == LOGIN
        session = self._get(request)
        if session is None and not login:
            return redirect(request, LOGIN)
        if request.method == "POST":
            origin = request.headers.getall("Origin", [])
            if origin != ["https://" + request.host]:
                return web.Response(status=403, text="表单来源无效，请刷新页面后重试。")
            if request.content_type != "application/x-www-form-urlencoded":
                return web.Response(status=415, text="不支持的表单格式。")
            request = request.clone(client_max_size=16 * 1024)
            if session is not None and not login:
                csrf = form_value(await request.post(), "csrf").encode("utf-8")
                if not compare_digest(csrf, session.csrf.encode("utf-8")):
                    return web.Response(status=403, text="表单已失效，请刷新页面后重试。")
        return await handler(request)
