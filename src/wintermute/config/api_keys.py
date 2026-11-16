"""服务持有的模型凭据。管理视图只公开用途和掩码，覆盖值不进入事件库。"""

import json
import os
import tempfile
from collections.abc import Callable
from functools import partial
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import TypedDict

from wintermute.config.models import AppConfig, ConfigError, DeepSeekASRSettings, ModelSettings
from wintermute.logging.setup import protect_secrets

type _Settings = ModelSettings | DeepSeekASRSettings


class APIKeyItem(TypedDict):
    id: str
    usages: list[str]
    configured: bool
    masked: str | None


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
    override = os.environ.get("WINTERMUTE_SETTINGS_DIR")
    if override:
        directory = Path(override).expanduser()
        if not directory.is_absolute():
            raise ConfigError("WINTERMUTE_SETTINGS_DIR must be absolute")
    else:
        if os.name == "nt":
            root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        else:
            root = Path(os.environ.get("XDG_CONFIG_HOME", ""))
            if not root.is_absolute():
                root = Path.home() / ".config"
        directory = root / "wintermute"
    # Keep independent installations isolated, outside both the event data and tool workspace.
    instance = sha256(os.fsencode(os.path.normcase(str(db)))).hexdigest()
    return directory / instance / "api-keys.json"


class APIKeyRegistry:
    def __init__(self, config: AppConfig, *, base_dir: str | Path = ".") -> None:
        self._lock = RLock()
        self._settings: dict[str, _Settings] = {}
        self._usage: dict[str, list[str]] = {}
        self._slot: dict[str, str] = {}
        http = config.interfaces.http
        self.can_provision = http.enabled and any(t.manage_api_keys for t in http.terminals)
        self._path: Path | None = None
        if config.events.database_path != ":memory:":
            db = (Path(base_dir) / Path(config.events.database_path).expanduser()).resolve()
            self._path = _private_file(db)

        def bind(slot: str, settings: _Settings, usage: str) -> None:
            variable = settings.api_key_env
            if variable and os.name == "nt":
                variable = variable.upper()
            key_id = f"env:{variable}" if variable and settings.api_key is None else f"slot:{slot}"
            self._slot[slot] = key_id
            self._settings.setdefault(key_id, settings)
            self._usage.setdefault(key_id, []).append(usage)

        bind("main", config.model, "主 Agent")
        for subagent in config.subagents:
            usage = "子 Agent · " + subagent.name
            if subagent.model is None:
                self._usage[self._slot["main"]].append(usage)
            else:
                bind(subagent_slot(subagent.name), subagent.model, usage)
        slices = config.events.slices
        if slices.enabled and slices.strategy == "model":
            self._usage[self._slot["main"]].append("切片摘要")
        if slices.audio.enabled:
            bind("asr", slices.audio.asr, "语音转录")
            if slices.audio.summary_model is not None:
                bind("audio-summary", slices.audio.summary_model, "音频摘要")
        if slices.image.enabled and slices.image.model is not None:
            bind("vision", slices.image.model, "截图识别")
        timeline = config.events.timeline_compression
        if timeline.ai_enabled:
            if timeline.summary_model is not None:
                bind("timeline", timeline.summary_model, "活动时间线压缩")
            else:
                fallback = "audio-summary" if "audio-summary" in self._slot else "main"
                self._usage[self._slot[fallback]].append("活动时间线压缩")
        profile = config.events.profile_dreaming
        if config.events.profile_files and profile.enabled:
            if profile.summary_model is not None:
                bind("profile", profile.summary_model, "用户档案更新")
            else:
                self._usage[self._slot["main"]].append("用户档案更新")

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

    def list_items(self) -> list[APIKeyItem]:
        with self._lock:
            items: list[APIKeyItem] = []
            for key_id, usages in self._usage.items():
                try:
                    value = self._resolve(key_id)
                except ConfigError:
                    value = ""
                suffix = value[-4:] if len(value) >= 8 and _valid(value) else ""
                items.append(
                    {
                        "id": key_id,
                        "usages": list(usages),
                        "configured": bool(value),
                        "masked": "••••" + suffix if value else None,
                    }
                )
            return items

    def update(self, key_id: str, api_key: str) -> list[APIKeyItem]:
        if key_id not in self._settings:
            raise KeyError("Unknown API key setting")
        if not _valid(api_key):
            raise ValueError("Invalid API key")
        with self._lock:
            updated = {**self._values, key_id: api_key}
            self._save(updated)
            protect_secrets((api_key,))
            self._values = updated
            return self.list_items()
