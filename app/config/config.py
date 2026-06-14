from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config/settings.json")
_SETTINGS_CACHE: Settings | None = None

# 画像模板随代码打包在 app/resource/profile/ 下，按包目录解析，不受工作目录影响。
_APP_DIR = Path(__file__).resolve().parent.parent
_PROFILE_RESOURCE_DIR = _APP_DIR / "resource" / "profile"

DEFAULT_SETTINGS = {
    "data_dir": "data",
    "log_dir": "logs",
    "log_retention_days": 7,
    "base_url": "https://api.openai.com/v1",
    "api_key": None,
    "model": None,
    "prompt_recent_turns": 5,
    "session_token_threshold": 12000,
    "prompt_token_budget": 24000,
    "scheduler_enabled": True,
    "profile_enabled": True,
    "soul_path": None,
    "persona_template_path": None,
    "user_template_path": None,
    "profile_max_tokens": 800,
    "tools_enabled": True,
    "max_tool_iterations": 5,
    "file_upload_poll_interval_seconds": 2,
    "file_upload_timeout_seconds": 600,
    "terminal_enabled": True,
    "terminal_workdir": "data/workspace",
    "terminal_timeout_seconds": 30,
    "terminal_command_denylist": [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "shutdown",
        "reboot",
        "init 0",
        "init 6",
        ":(){:|:&};:",
    ],
    "interfaces": {},
    "flows": {
        "L0": {"inputs": [], "outputs": [], "wait_for_result": True},
        "L1": {"inputs": [], "outputs": [], "wait_for_result": False},
        "L2": {"inputs": [], "outputs": [], "wait_for_result": False},
        "L3": {"inputs": [], "outputs": [], "wait_for_result": False},
    },
}


@dataclass(frozen=True)
class InterfaceSettings:
    """一个外部接口的配置。config 保存该接口类型自己的字段。"""

    name: str
    type: str
    enabled: bool
    config: dict[str, Any]


@dataclass(frozen=True)
class FlowSettings:
    """一个注意力层的输入输出配置。"""

    level: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    wait_for_result: bool


@dataclass(frozen=True)
class Settings:
    """应用运行配置，来源包括默认值和 JSON 配置文件。"""

    data_dir: Path
    log_dir: Path
    log_retention_days: int
    base_url: str
    api_key: str | None
    model: str | None
    prompt_recent_turns: int
    session_token_threshold: int
    prompt_token_budget: int
    scheduler_enabled: bool
    profile_enabled: bool
    soul_path: Path
    persona_template_path: Path
    user_template_path: Path
    profile_max_tokens: int
    tools_enabled: bool
    max_tool_iterations: int
    file_upload_poll_interval_seconds: int
    file_upload_timeout_seconds: int
    terminal_enabled: bool
    terminal_workdir: Path
    terminal_timeout_seconds: int
    terminal_command_denylist: tuple[str, ...]
    interfaces: dict[str, InterfaceSettings]
    flows: dict[str, FlowSettings]

    def __post_init__(self) -> None:
        """配置对象创建后，自动确保运行所需目录存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, config_path: Path | str = DEFAULT_CONFIG_PATH) -> "Settings":
        """加载配置；优先级为 JSON 配置文件 > 默认值。"""
        values = dict(DEFAULT_SETTINGS)
        values.update(_read_json_config(Path(config_path)))

        model = _optional_str(values.get("model"))

        return cls(
            data_dir=Path(str(values["data_dir"])),
            log_dir=Path(str(values["log_dir"])),
            log_retention_days=_as_int(values.get("log_retention_days"), default=7),
            base_url=str(values["base_url"]),
            api_key=_optional_str(values.get("api_key")),
            model=model,
            prompt_recent_turns=_as_int(values.get("prompt_recent_turns"), default=5),
            session_token_threshold=_as_int(
                values.get("session_token_threshold"),
                default=12000,
            ),
            prompt_token_budget=_as_int(values.get("prompt_token_budget"), default=24000),
            scheduler_enabled=_as_bool(values.get("scheduler_enabled"), default=True),
            profile_enabled=_as_bool(values.get("profile_enabled"), default=True),
            soul_path=_resolve_path(
                values.get("soul_path"), _PROFILE_RESOURCE_DIR / "soul.md"
            ),
            persona_template_path=_resolve_path(
                values.get("persona_template_path"), _PROFILE_RESOURCE_DIR / "persona.md"
            ),
            user_template_path=_resolve_path(
                values.get("user_template_path"), _PROFILE_RESOURCE_DIR / "user.md"
            ),
            profile_max_tokens=_as_int(values.get("profile_max_tokens"), default=800),
            tools_enabled=_as_bool(values.get("tools_enabled"), default=True),
            max_tool_iterations=_as_int(values.get("max_tool_iterations"), default=5),
            file_upload_poll_interval_seconds=_as_int(
                values.get("file_upload_poll_interval_seconds"), default=2
            ),
            file_upload_timeout_seconds=_as_int(
                values.get("file_upload_timeout_seconds"), default=600
            ),
            terminal_enabled=_as_bool(values.get("terminal_enabled"), default=True),
            terminal_workdir=Path(str(values.get("terminal_workdir") or "data/workspace")),
            terminal_timeout_seconds=_as_int(
                values.get("terminal_timeout_seconds"), default=30
            ),
            terminal_command_denylist=tuple(
                str(item)
                for item in (values.get("terminal_command_denylist") or [])
                if str(item).strip()
            ),
            interfaces=_load_interfaces(values.get("interfaces")),
            flows=_load_flows(values.get("flows")),
        )


def get_settings() -> Settings:
    """读取并缓存全局运行配置。"""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = Settings.load(DEFAULT_CONFIG_PATH)
    return _SETTINGS_CACHE


def reset_settings_cache() -> None:
    """清理全局配置缓存，供测试隔离不同配置文件。"""
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def _read_json_config(path: Path) -> dict[str, object]:
    """读取 JSON 配置文件；文件不存在时视为空配置。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 JSON 对象: {path}")
    return data


def _load_interfaces(value: object) -> dict[str, InterfaceSettings]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("interfaces 必须是 JSON 对象。")
    interfaces: dict[str, InterfaceSettings] = {}
    for name, raw in value.items():
        interface_name = str(name).strip()
        if not interface_name:
            raise ValueError("interfaces 的键不能为空。")
        if not isinstance(raw, dict):
            raise ValueError(f"接口配置必须是 JSON 对象: {interface_name}")
        interface_type = _optional_str(raw.get("type")) or interface_name
        enabled = _as_bool(raw.get("enabled"), default=False)
        config = {
            str(key): item
            for key, item in raw.items()
            if key not in {"type", "enabled"}
        }
        interfaces[interface_name] = InterfaceSettings(
            name=interface_name,
            type=interface_type,
            enabled=enabled,
            config=config,
        )
    return interfaces


def _load_flows(value: object) -> dict[str, FlowSettings]:
    default_flows = DEFAULT_SETTINGS["flows"]
    if not isinstance(default_flows, dict):
        raise ValueError("默认 flows 配置无效。")
    merged: dict[str, dict[str, Any]] = {
        str(level): dict(config)
        for level, config in default_flows.items()
        if isinstance(config, dict)
    }
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("flows 必须是 JSON 对象。")
        for level, raw in value.items():
            flow_level = str(level).strip().upper()
            if flow_level not in merged:
                raise ValueError(f"未知流程层级: {flow_level}")
            if not isinstance(raw, dict):
                raise ValueError(f"流程配置必须是 JSON 对象: {flow_level}")
            next_config = dict(merged[flow_level])
            next_config.update(raw)
            merged[flow_level] = next_config

    flows: dict[str, FlowSettings] = {}
    for level, raw in merged.items():
        flows[level] = FlowSettings(
            level=level,
            inputs=_as_string_tuple(raw.get("inputs")),
            outputs=_as_string_tuple(raw.get("outputs")),
            wait_for_result=_as_bool(
                raw.get("wait_for_result"),
                default=(level == "L0"),
            ),
        )
    return flows


def _as_string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list | tuple):
        raise ValueError("配置值必须是字符串数组。")
    items = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return tuple(items)


def _resolve_path(value: object, default: Path) -> Path:
    """配置提供了路径就用它，否则回退到随包打包的默认资源路径。"""
    text = _optional_str(value)
    return Path(text) if text else default


def _optional_str(value: object) -> str | None:
    """把可选配置值规范成非空字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: object, *, default: int) -> int:
    """把配置值转换成整数；空值使用默认值。"""
    if value is None or value == "":
        return default
    return int(value)


def _as_bool(value: object, *, default: bool) -> bool:
    """把配置值转换成布尔值；空值使用默认值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置: {value}")
