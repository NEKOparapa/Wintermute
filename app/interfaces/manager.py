from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from ..attention.attention import AttentionLevel
from ..config.config import InterfaceSettings
from ..flows.flow_runtime import (
    FlowConfig,
    FlowSubmitRequest,
    FlowSubmitResult,
    InterfaceAdapter,
    InterfaceOutput,
    RuntimeConfigError,
    input_levels_by_interface,
)
from .telegram import TelegramAdapter


class InterfaceManager:
    """统一管理外部接口适配器的生命周期和输出路由。"""

    def __init__(self, adapters: Mapping[str, InterfaceAdapter]) -> None:
        self._adapters = dict(adapters)

    @classmethod
    def from_settings(
        cls,
        interfaces: Mapping[str, InterfaceSettings],
        flow_configs: dict[AttentionLevel, FlowConfig],
    ) -> "InterfaceManager":
        """按配置构建并校验已启用的外部接口适配器。"""
        input_levels = input_levels_by_interface(flow_configs)
        adapters: dict[str, InterfaceAdapter] = {}
        telegram_tokens: dict[str, str] = {}

        for name, settings in interfaces.items():
            if not settings.enabled:
                continue
            if settings.type != "telegram":
                raise RuntimeConfigError(f"暂不支持的接口类型: {settings.type}")

            token = _required_str(settings.config.get("bot_token"), f"{name}.bot_token")
            if token in telegram_tokens:
                previous = telegram_tokens[token]
                raise RuntimeConfigError(
                    f"Telegram bot_token 同时配置给 {previous} 和 {name}"
                )
            telegram_tokens[token] = name

            adapters[name] = TelegramAdapter(
                name=name,
                bot_token=token,
                input_level=input_levels.get(name),
                allowed_chat_ids=_string_tuple(settings.config.get("allowed_chat_ids")),
                poll_interval_seconds=_float(
                    settings.config.get("poll_interval_seconds"),
                    default=1.0,
                ),
                request_timeout_seconds=_float(
                    settings.config.get("request_timeout_seconds"),
                    default=30.0,
                ),
            )
        return cls(adapters)

    @property
    def names(self) -> frozenset[str]:
        """返回已启用接口名集合。"""
        return frozenset(self._adapters)

    def start(self, submit: Callable[[FlowSubmitRequest], FlowSubmitResult]) -> None:
        """启动所有接口输入监听。"""
        for adapter in self._adapters.values():
            adapter.start(submit)

    def stop(self) -> None:
        """停止所有接口。"""
        for adapter in self._adapters.values():
            adapter.stop()

    def send(self, output: InterfaceOutput) -> None:
        """把流程输出路由给目标接口。"""
        adapter = self._adapters.get(output.interface)
        if adapter is None:
            raise RuntimeConfigError(f"输出接口未注册: {output.interface}")
        adapter.send(output)


def _required_str(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeConfigError(f"接口配置缺少 {label}")
    return text


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list | tuple):
        raise RuntimeConfigError("allowed_chat_ids 必须是字符串数组。")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _float(value: object, *, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)
