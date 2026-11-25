"""服务持有的模型凭据。管理视图只公开用途和掩码，覆盖值不进入事件库。"""

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from pathlib import Path
from threading import Lock

from purin.config.models import (
    AppConfig,
    ConfigError,
    DeepSeekASRSettings,
    DeepSeekWebSettings,
    ModelSettings,
)
from purin.logging.setup import protect_secrets

type _Settings = ModelSettings | DeepSeekASRSettings | DeepSeekWebSettings


@dataclass(frozen=True, slots=True)
class APIKeyUsage:
    name: str
    group: str
    endpoint: str
    key_id: str
    model: str | None = None
    inherits_main: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class APIKeyDetails:
    configured: bool
    masked: str | None
    source: str
    environment: str | None


def _valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and value.isascii()
        and value.isprintable()
        and " " not in value
    )


def subagent_slot(name: str) -> str:
    return "subagent-" + sha256(name.encode()).hexdigest()


def _private_file(db: Path) -> Path:
    override = os.environ.get("PURIN_SETTINGS_DIR")
    if override:
        directory = Path(override).expanduser()
        if not directory.is_absolute():
            raise ConfigError("PURIN_SETTINGS_DIR must be absolute")
    else:
        if os.name == "nt":
            root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        else:
            root = Path(os.environ.get("XDG_CONFIG_HOME", ""))
            if not root.is_absolute():
                root = Path.home() / ".config"
        directory = root / "purin"
    # Keep independent installations isolated, outside both the event data and tool workspace.
    instance = sha256(os.fsencode(os.path.normcase(str(db)))).hexdigest()
    return directory / instance / "api-keys.json"


class APIKeyRegistry:
    def __init__(self, config: AppConfig, *, base_dir: str | Path = ".") -> None:
        self._lock = Lock()
        self._settings: dict[str, _Settings] = {}
        self._slot: dict[str, str] = {}
        usages: list[APIKeyUsage] = []
        http = config.interfaces.http
        self.can_provision = http.enabled and http.admin.enabled
        self._path: Path | None = None
        if config.events.database_path != ":memory:":
            db = (Path(base_dir) / Path(config.events.database_path).expanduser()).resolve()
            self._path = _private_file(db)

        def bind(slot: str, settings: _Settings) -> str:
            variable = settings.api_key_env
            if variable and os.name == "nt":
                variable = variable.upper()
            key_id = f"env:{variable}" if variable and settings.api_key is None else f"slot:{slot}"
            self._slot[slot] = key_id
            self._settings.setdefault(key_id, settings)
            return key_id

        def model_usage(
            slot: str, settings: ModelSettings | None, name: str, group: str = "模型调用"
        ) -> None:
            inherits = settings is None
            if settings is None:
                settings = config.model
                key_id = self._slot["main"]
            else:
                key_id = bind(slot, settings)
            path = "responses" if settings.api == "responses" else "chat/completions"
            usages.append(
                APIKeyUsage(
                    name,
                    group,
                    f"{settings.base_url.rstrip('/')}/{path}",
                    key_id,
                    model=settings.model,
                    inherits_main=inherits,
                )
            )

        model_usage("main", config.model, "主 Agent")
        if config.tools.web.enabled:
            web = config.tools.web
            key_id = bind("web", web)
            usages.extend(
                (
                    APIKeyUsage("网络搜索", "联网工具", f"{web.base_url}/tools/search", key_id),
                    APIKeyUsage("网页读取", "联网工具", f"{web.base_url}/tools/fetch", key_id),
                )
            )
        for subagent in config.subagents:
            model_usage(subagent_slot(subagent.name), subagent.model, "子 Agent · " + subagent.name)
        slices = config.events.slices
        if slices.audio.enabled or http.enabled:
            key_id = bind("asr", slices.audio.asr)
            audio_uses: list[str] = []
            if http.enabled:
                audio_uses.extend(("聊天语音", "音频附件"))
            if slices.enabled and slices.audio.enabled:
                audio_uses.append("录音采集")
            if audio_uses:
                usages.append(
                    APIKeyUsage(
                        "语音转录",
                        "语音与图像",
                        slices.audio.asr.endpoint,
                        key_id,
                        detail="、".join(audio_uses),
                    )
                )
        if slices.image.enabled and slices.image.model is not None:
            if slices.enabled:
                model_usage("vision", slices.image.model, "截图识别", "语音与图像")
            else:
                bind("vision", slices.image.model)
        timeline = config.events.timeline_compression
        if timeline.ai_enabled:
            model_usage("timeline", timeline.summary_model, "活动时间线压缩")
        memory = config.events.memory
        if memory.enabled and memory.user_dirs:
            model_usage("memory", memory.model, "记忆档案整理")

        self._calls = tuple(usages)
        self._values: dict[str, str] = self._load()
        protect_secrets(self._values.values())

    def _load(self) -> dict[str, str]:
        if self._path is None:
            return {}
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise ConfigError("Cannot read saved API key settings") from None
        if not isinstance(saved, dict) or not all(_valid(v) for v in saved.values()):
            raise ConfigError("Invalid saved API key settings")
        return saved

    def _save(self, values: dict[str, str]) -> None:
        if self._path is None:
            return
        directory = self._path.parent
        # Private directory also provides the inheritable ACL on Windows.
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=directory)
        temp = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._path)
        finally:
            temp.unlink(missing_ok=True)

    def _resolve(self, key_id: str) -> str:
        # _values is published as an immutable snapshot only after the atomic rename.
        value = self._values.get(key_id)
        return value if value is not None else self._settings[key_id].resolve_api_key()

    def provider(self, slot: str) -> Callable[[], str]:
        """每次发起请求时，从同一注册表获取当前的凭据。"""
        return partial(self._resolve, self._slot[slot])

    def admin_overview(self) -> tuple[tuple[APIKeyUsage, ...], dict[str, APIKeyDetails]]:
        with self._lock:
            details = {}
            for key_id, settings in self._settings.items():
                try:
                    value = self._resolve(key_id)
                except ConfigError:
                    value = ""
                suffix = value[-4:] if len(value) >= 8 and _valid(value) else ""
                masked = "••••" + suffix if value else None
                env = key_id.removeprefix("env:") if key_id.startswith("env:") else None
                if key_id in self._values:
                    source = "密钥设置"
                elif env:
                    source = "环境变量"
                elif settings.api_key is not None:
                    source = "配置文件"
                else:
                    source = "待设置"
                details[key_id] = APIKeyDetails(bool(value), masked, source, env)
            return self._calls, details

    def update(self, key_id: str, api_key: str) -> None:
        if key_id not in self._settings:
            raise KeyError("Unknown API key setting")
        if not _valid(api_key):
            raise ValueError("Invalid API key")
        with self._lock:
            updated = {**self._values, key_id: api_key}
            self._save(updated)
            protect_secrets((api_key,))
            self._values = updated
