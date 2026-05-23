from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/settings.json")

DEFAULT_SETTINGS = {
    "data_dir": "data",
    "log_dir": "logs",
    "log_retention_days": 7,
    "host": "127.0.0.1",
    "port": 8000,
    "base_url": "https://api.openai.com/v1",
    "api_key": None,
    "model": None,
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
        )


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
