from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config/settings.json")
INTERFACE_CONFIG_DIR_NAME = "interfaces"
INTERFACE_CONFIG_FILE_NAME = "settings.json"
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
}

DEFAULT_INTERFACE_SETTINGS = {
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
        config_path = Path(config_path)
        raw_values = _read_json_config(config_path)

        values = dict(DEFAULT_SETTINGS)
        values.update(raw_values)
        interface_values = _read_interface_json_config(config_path)

        return cls(
            data_dir=Path(values["data_dir"]),
            log_dir=Path(values["log_dir"]),
            log_retention_days=values["log_retention_days"],
            base_url=values["base_url"],
            api_key=values["api_key"],
            model=values["model"],
            prompt_recent_turns=values["prompt_recent_turns"],
            session_token_threshold=values["session_token_threshold"],
            prompt_token_budget=values["prompt_token_budget"],
            scheduler_enabled=values["scheduler_enabled"],
            profile_enabled=values["profile_enabled"],
            soul_path=_resolve_path(
                values.get("soul_path"), _PROFILE_RESOURCE_DIR / "soul.md"
            ),
            user_template_path=_resolve_path(
                values.get("user_template_path"), _PROFILE_RESOURCE_DIR / "user.md"
            ),
            profile_max_tokens=values["profile_max_tokens"],
            tools_enabled=values["tools_enabled"],
            max_tool_iterations=values["max_tool_iterations"],
            file_upload_poll_interval_seconds=values["file_upload_poll_interval_seconds"],
            file_upload_timeout_seconds=values["file_upload_timeout_seconds"],
            terminal_enabled=values["terminal_enabled"],
            terminal_workdir=Path(values["terminal_workdir"]),
            terminal_timeout_seconds=values["terminal_timeout_seconds"],
            terminal_command_denylist=tuple(values["terminal_command_denylist"]),
            interfaces=_load_interfaces(interface_values.get("interfaces")),
            flows=_load_flows(interface_values.get("flows")),
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


def _read_json_config(path: Path) -> dict[str, Any]:
    """读取 JSON 配置文件；文件不存在时视为空配置。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _read_interface_json_config(config_path: Path) -> dict[str, Any]:
    """读取接口配置，并确保接口配置目录存在。"""
    interface_dir = config_path.parent / INTERFACE_CONFIG_DIR_NAME
    interface_dir.mkdir(parents=True, exist_ok=True)
    return _read_json_config(interface_dir / INTERFACE_CONFIG_FILE_NAME)


def _load_interfaces(value: Any) -> dict[str, InterfaceSettings]:
    if value is None:
        return {}
    interfaces: dict[str, InterfaceSettings] = {}
    for name, raw in value.items():
        interface_name = name
        config = {
            key: item
            for key, item in raw.items()
            if key not in {"type", "enabled"}
        }
        interfaces[interface_name] = InterfaceSettings(
            name=interface_name,
            type=raw["type"],
            enabled=raw["enabled"],
            config=config,
        )
    return interfaces


def _load_flows(value: Any) -> dict[str, FlowSettings]:
    default_flows = DEFAULT_INTERFACE_SETTINGS["flows"]
    merged: dict[str, dict[str, Any]] = {
        str(level): dict(config)
        for level, config in default_flows.items()
    }
    if value is not None:
        for level, raw in value.items():
            next_config = dict(merged.get(level, {}))
            next_config.update(raw)
            merged[level] = next_config

    flows: dict[str, FlowSettings] = {}
    for level, raw in merged.items():
        flows[level] = FlowSettings(
            level=level,
            inputs=_string_tuple(raw.get("inputs")),
            outputs=_string_tuple(raw.get("outputs")),
            wait_for_result=raw.get("wait_for_result", level == "L0"),
        )
    return flows


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list | tuple):
        value = (value,)
    return tuple(text for item in value if (text := str(item).strip()))


def _resolve_path(value: Any, default: Path) -> Path:
    """配置提供了路径就用它，否则回退到随包打包的默认资源路径。"""
    return Path(value) if value is not None else default
