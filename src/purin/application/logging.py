"""从应用配置收集日志设置与需要脱敏的凭据。"""

import os
from pathlib import Path

from purin.config.models import (
    AppConfig,
)
from purin.logging import LoggingHandle, configure_logging


def configure_app_logging(config: AppConfig, *, base_dir: str | Path = ".") -> LoggingHandle:
    """统一应用日志设置，并将当前配置使用的凭据加入日志脱敏集合。"""
    secrets: list[str] = []
    for model in (
        config.model,
        *(item.model for item in config.subagents),
        config.events.slices.image.model,
        config.events.timeline_compression.summary_model,
        config.events.memory.model,
    ):
        if model is None:
            continue
        if model.api_key is not None:
            secrets.append(model.api_key.get_secret_value())
        if model.api_key_env:
            value = os.environ.get(model.api_key_env)
            if value:
                secrets.append(value)
    credentials = [
        (device.token, device.token_env) for device in config.interfaces.http.devices
    ]
    asr = config.events.slices.audio.asr
    credentials.append((asr.api_key, asr.api_key_env))
    web = config.tools.web
    credentials.append((web.api_key, web.api_key_env))
    for secret, variable in credentials:
        if secret is not None:
            secrets.append(secret.get_secret_value())
        if variable and (value := os.environ.get(variable)):
            secrets.append(value)
    return configure_logging(config.logging, base_dir=base_dir, secrets=secrets)
