from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/settings.json")

DEFAULT_SETTINGS = {
    "data_dir": "data",
    "log_dir": "logs",
    "log_retention_days": 7,
    "base_url": "https://api.openai.com/v1",
    "api_key": None,
    "model": None,
}

ENV_OVERRIDES = {
    "data_dir": "WINTERMUTE_DATA_DIR",
    "log_dir": "WINTERMUTE_LOG_DIR",
    "log_retention_days": "WINTERMUTE_LOG_RETENTION_DAYS",
    "base_url": "WINTERMUTE_BASE_URL",
    "api_key": "WINTERMUTE_API_KEY",
    "model": "WINTERMUTE_MODEL",
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_dir: Path
    log_retention_days: int
    base_url: str
    api_key: str | None
    model: str | None

    @classmethod
    def load(cls, config_path: Path | str = DEFAULT_CONFIG_PATH) -> "Settings":
        values = dict(DEFAULT_SETTINGS)
        values.update(_read_json_config(Path(config_path)))
        for key, env_name in ENV_OVERRIDES.items():
            if env_name in os.environ:
                values[key] = os.environ[env_name]

        model = _optional_str(values.get("model"))

        return cls(
            data_dir=Path(str(values["data_dir"])),
            log_dir=Path(str(values["log_dir"])),
            log_retention_days=_as_int(values.get("log_retention_days"), default=7),
            base_url=str(values["base_url"]),
            api_key=_optional_str(values.get("api_key")),
            model=model,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.load()


def _read_json_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 JSON 对象: {path}")
    return data


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)
