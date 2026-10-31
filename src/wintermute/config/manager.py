"""JSON 配置加载、原子保存与显式重新加载。"""

import json
import os
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import SecretStr, ValidationError

from wintermute.config.models import (
    AgentSettings,
    AppConfig,
    AudioSliceSettings,
    AudioSummaryModelSettings,
    BackgroundSettings,
    ConfigError,
    ContextSettings,
    ConversationBindingSettings,
    EventSettings,
    HTTPSettings,
    HTTPTerminalSettings,
    InterfacesSettings,
    LoggingSettings,
    ModelSettings,
    SliceSettings,
    SubagentSettings,
    TelegramBindingSettings,
    TelegramSettings,
    ToolsSettings,
    VolcASRSettings,
)

_FIELD_NAMES = frozenset(
    name
    for model in (
        AppConfig,
        AudioSliceSettings,
        AudioSummaryModelSettings,
        AgentSettings,
        BackgroundSettings,
        ContextSettings,
        EventSettings,
        LoggingSettings,
        ModelSettings,
        SliceSettings,
        SubagentSettings,
        ToolsSettings,
        ConversationBindingSettings,
        HTTPTerminalSettings,
        HTTPSettings,
        TelegramBindingSettings,
        TelegramSettings,
        InterfacesSettings,
        VolcASRSettings,
    )
    for name in model.model_fields
)


def _plain(value: Any) -> Any:
    """持久化显式填写的原始密钥；展示用途应调用 redacted_dict。"""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _validate(data: Any) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        # 不输出输入内容或自定义校验上下文；其中可能含有凭据。
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        locations = [
            ".".join(
                str(part) if isinstance(part, int) or part in _FIELD_NAMES else "<field>"
                for part in error["loc"]
            )
            or "configuration"
            for error in errors
        ]
        raise ConfigError("Invalid configuration fields: " + ", ".join(locations)) from None


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 不回显重复键，避免用户把敏感值误写成键名时泄漏。
            raise ConfigError("JSON configuration contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ConfigError("JSON configuration requires finite numbers")


def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        previous = merged.get(key)
        if isinstance(value, Mapping) and isinstance(previous, dict):
            merged[key] = _merge(previous, value)
        else:
            merged[key] = deepcopy(value)
    return merged


class ConfigManager:
    """一个 JSON 文件的配置快照；保存成功后才替换内存状态。

    路径相对于配置文件目录解析。实例内读写使用可重入锁；多个进程之间
    只保证文件替换原子性，不提供多写入者的补丁合并或自动文件监控。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.base_dir = self.path.parent
        self._config: AppConfig | None = None
        self._lock = RLock()

    @property
    def config(self) -> AppConfig:
        with self._lock:
            if self._config is None:
                raise ConfigError("Configuration has not been loaded or saved")
            return self._config.model_copy(deep=True)

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.base_dir / path).resolve()

    def load(self) -> AppConfig:
        with self._lock:
            try:
                data = json.loads(
                    self.path.read_text(encoding="utf-8-sig"),
                    object_pairs_hook=_object_pairs,
                    parse_constant=_reject_constant,
                )
            except (OSError, UnicodeError):
                raise ConfigError("Unable to read configuration file") from None
            except (ValueError, RecursionError) as exc:
                if isinstance(exc, ConfigError):
                    raise
                raise ConfigError("Configuration file is not valid JSON") from None
            config = _validate(data)
            self._config = config
            return config.model_copy(deep=True)

    def reload(self) -> AppConfig:
        return self.load()

    def save(self, config: AppConfig) -> None:
        with self._lock:
            # frozen 不会冻结内部列表或字典，落盘前验证深拷贝以防绕过模型校验。
            try:
                validated = _validate(_plain(config.model_dump(mode="python", warnings=False)))
                content = (
                    json.dumps(
                        _plain(validated.model_dump(mode="python")),
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                    + "\n"
                )
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ConfigError):
                    raise
                raise ConfigError("Configuration cannot be serialized") from None
            self._write(content)
            self._config = validated

    def _write(self, content: str) -> None:
        temporary: Path | None = None
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.base_dir,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, UnicodeError):
            raise ConfigError("Unable to atomically save configuration file") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 清理失败不掩盖原来的保存异常，旧配置仍未被替换。
                    pass

    def update(self, patch: Mapping[str, Any]) -> AppConfig:
        """递归合并对象，整体替换列表；校验和保存成功后返回新快照。"""
        with self._lock:
            if self._config is None:
                self.load()
            current = self.config
            merged = _merge(_plain(current.model_dump(mode="python")), patch)
            updated = _validate(merged)
            self.save(updated)
            return self.config


def load_config(path: str | Path) -> AppConfig:
    return ConfigManager(path).load()


def save_config(config: AppConfig, path: str | Path) -> None:
    ConfigManager(path).save(config)
