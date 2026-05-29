from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/settings.json")
_SETTINGS_CACHE: Settings | None = None

DEFAULT_SETTINGS = {
    "data_dir": "data",
    "log_dir": "logs",
    "log_retention_days": 7,
    "host": "127.0.0.1",
    "port": 8000,
    "base_url": "https://api.openai.com/v1",
    "api_key": None,
    "model": None,
    "prompt_recent_turns": 5,
    "session_token_threshold": 12000,
    "prompt_token_budget": 24000,
    "scheduler_enabled": True,
    "profile_enabled": True,
    "soul_path": "config/soul.md",
    "persona_template_path": "config/persona.md",
    "user_template_path": "config/user.md",
    "profile_max_tokens": 800,
    "tools_enabled": True,
    "max_tool_iterations": 5,
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


@dataclass(frozen=True)
class Settings:
    """应用运行配置，来源包括默认值和 JSON 配置文件。"""

    data_dir: Path
    log_dir: Path
    log_retention_days: int
    host: str
    port: int
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
    terminal_enabled: bool
    terminal_workdir: Path
    terminal_timeout_seconds: int
    terminal_command_denylist: tuple[str, ...]

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
            host=str(values["host"]),
            port=_as_int(values.get("port"), default=8000),
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
            soul_path=Path(str(values.get("soul_path") or "config/soul.md")),
            persona_template_path=Path(
                str(values.get("persona_template_path") or "config/persona.md")
            ),
            user_template_path=Path(
                str(values.get("user_template_path") or "config/user.md")
            ),
            profile_max_tokens=_as_int(values.get("profile_max_tokens"), default=800),
            tools_enabled=_as_bool(values.get("tools_enabled"), default=True),
            max_tool_iterations=_as_int(values.get("max_tool_iterations"), default=5),
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
