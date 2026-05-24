from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/settings.json")

# 默认配置。键都用扁平命名,前缀区分用途,JSON 改起来一目了然。
DEFAULT_SETTINGS = {
    # ---- 基础设施 ----
    "data_dir": "data",
    "log_dir": "logs",
    "log_retention_days": 7,
    "host": "127.0.0.1",
    "port": 8000,

    # ---- LLM ----
    "base_url": "https://api.openai.com/v1",
    "api_key": None,
    "model": None,

    # ---- 短期消息窗口 ----
    "recent_rounds": 5,

    # ---- session 压缩触发 ----
    "session_compress_trigger_tokens": 4000,

    # ---- prompt 各层级注入预算 (tokens) ----
    "prompt_budget_session_tokens": 8000,
    "prompt_budget_daily_tokens": 8000,
    "prompt_budget_weekly_tokens": 4000,
    "prompt_budget_monthly_tokens": 2000,

    # ---- 调度器时间表 (系统本地时间) ----
    "daily_rollup_hour": 3,
    "daily_rollup_minute": 0,
    # 周一 = 0,周日 = 6 (Python datetime.weekday() 约定)
    "weekly_rollup_weekday": 0,
    "weekly_rollup_hour": 3,
    "weekly_rollup_minute": 30,
    "monthly_rollup_day": 1,
    "monthly_rollup_hour": 4,
    "monthly_rollup_minute": 0,
}


@dataclass(frozen=True)
class Settings:
    """应用运行配置,来源包括默认值和 JSON 配置文件。"""

    # 基础设施
    data_dir: Path
    log_dir: Path
    log_retention_days: int
    host: str
    port: int

    # LLM
    base_url: str
    api_key: str | None
    model: str | None

    # 短期消息窗口
    recent_rounds: int

    # session 压缩触发
    session_compress_trigger_tokens: int

    # prompt 各层注入预算
    prompt_budget_session_tokens: int
    prompt_budget_daily_tokens: int
    prompt_budget_weekly_tokens: int
    prompt_budget_monthly_tokens: int

    # 调度器时间表
    daily_rollup_hour: int
    daily_rollup_minute: int
    weekly_rollup_weekday: int
    weekly_rollup_hour: int
    weekly_rollup_minute: int
    monthly_rollup_day: int
    monthly_rollup_hour: int
    monthly_rollup_minute: int

    def __post_init__(self) -> None:
        """配置对象创建后,自动确保运行所需目录存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, config_path: Path | str = DEFAULT_CONFIG_PATH) -> "Settings":
        """加载配置;优先级为 JSON 配置文件 > 默认值。"""
        values = dict(DEFAULT_SETTINGS)
        values.update(_read_json_config(Path(config_path)))

        return cls(
            data_dir=Path(str(values["data_dir"])),
            log_dir=Path(str(values["log_dir"])),
            log_retention_days=_as_int(values.get("log_retention_days"), default=7),
            host=str(values["host"]),
            port=_as_int(values.get("port"), default=8000),
            base_url=str(values["base_url"]),
            api_key=_optional_str(values.get("api_key")),
            model=_optional_str(values.get("model")),
            recent_rounds=_as_int(values.get("recent_rounds"), default=5),
            session_compress_trigger_tokens=_as_int(
                values.get("session_compress_trigger_tokens"), default=4000
            ),
            prompt_budget_session_tokens=_as_int(
                values.get("prompt_budget_session_tokens"), default=8000
            ),
            prompt_budget_daily_tokens=_as_int(
                values.get("prompt_budget_daily_tokens"), default=8000
            ),
            prompt_budget_weekly_tokens=_as_int(
                values.get("prompt_budget_weekly_tokens"), default=4000
            ),
            prompt_budget_monthly_tokens=_as_int(
                values.get("prompt_budget_monthly_tokens"), default=2000
            ),
            daily_rollup_hour=_as_int(values.get("daily_rollup_hour"), default=3),
            daily_rollup_minute=_as_int(values.get("daily_rollup_minute"), default=0),
            weekly_rollup_weekday=_as_int(values.get("weekly_rollup_weekday"), default=0),
            weekly_rollup_hour=_as_int(values.get("weekly_rollup_hour"), default=3),
            weekly_rollup_minute=_as_int(values.get("weekly_rollup_minute"), default=30),
            monthly_rollup_day=_as_int(values.get("monthly_rollup_day"), default=1),
            monthly_rollup_hour=_as_int(values.get("monthly_rollup_hour"), default=4),
            monthly_rollup_minute=_as_int(values.get("monthly_rollup_minute"), default=0),
        )


def _read_json_config(path: Path) -> dict[str, object]:
    """读取 JSON 配置文件;文件不存在时视为空配置。"""
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
    """把配置值转换成整数;空值使用默认值。"""
    if value is None or value == "":
        return default
    return int(value)
