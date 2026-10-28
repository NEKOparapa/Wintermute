"""显式配置 Wintermute 自己的日志，不接管宿主应用的根日志器。"""

import logging
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Self, TextIO

from wintermute.logging.formatters import JSONFormatter, TextFormatter

if TYPE_CHECKING:
    from wintermute.config.models import LoggingSettings

_configuration_lock = RLock()
_active_handle: "LoggingHandle | None" = None


class _SafeErrors:
    def handleError(self, record: logging.LogRecord) -> None:
        """写入失败时避免标准库将未经脱敏的 msg/args 输出到 stderr。"""
        if sys.stderr is not None:
            try:
                sys.stderr.write("Wintermute 日志写入失败。\n")
            except Exception:
                pass


class _ConsoleHandler(_SafeErrors, logging.StreamHandler[TextIO]):
    pass


class _RotatingHandler(_SafeErrors, RotatingFileHandler):
    pass


class LoggingHandle:
    """管理本次配置创建的处理器；重复关闭或关闭被替换的配置均安全。"""

    def __init__(
        self,
        logger: logging.Logger,
        handlers: tuple[logging.Handler, ...],
        previous_state: tuple[int, bool, bool],
    ) -> None:
        self._logger = logger
        self._handlers = handlers
        self._previous_state = previous_state
        self._closed = False

    @property
    def handlers(self) -> tuple[logging.Handler, ...]:
        return self._handlers

    def _remove_handlers(self) -> None:
        for handler in self._handlers:
            self._logger.removeHandler(handler)
            handler.close()
        self._closed = True

    def close(self) -> None:
        global _active_handle
        with _configuration_lock:
            if self._closed:
                return
            self._remove_handlers()
            if _active_handle is self:
                level, propagate, disabled = self._previous_state
                self._logger.setLevel(level)
                self._logger.propagate = propagate
                self._logger.disabled = disabled
                _active_handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def get_logger(name: str = "wintermute") -> logging.Logger:
    """完整 Wintermute 名称原样使用，短名称归入 wintermute 命名空间。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Logger name must be nonempty")
    name = name.strip()
    if name != "wintermute" and not name.startswith("wintermute."):
        name = f"wintermute.{name}"
    return logging.getLogger(name)


def configure_logging(
    settings: "LoggingSettings",
    *,
    base_dir: str | Path = ".",
    secrets: Iterable[str] = (),
) -> LoggingHandle:
    """配置控制台及轮转文件；相对文件路径基于 base_dir。

    新处理器创建成功后才替换上一份配置。保留宿主设置的处理器，关闭时
    恢复首次配置前的级别和传播状态。导入本模块不会自动调用此函数。
    """
    global _active_handle
    formatter = (JSONFormatter if settings.format == "json" else TextFormatter)(secrets)
    level = logging.getLevelNamesMapping()[settings.level]
    handlers: list[logging.Handler] = []
    with _configuration_lock:
        try:
            if settings.console:
                handlers.append(_ConsoleHandler(sys.stderr))
            if settings.file is not None:
                path = Path(settings.file).expanduser()
                if not path.is_absolute():
                    path = Path(base_dir).expanduser().resolve() / path
                path.parent.mkdir(parents=True, exist_ok=True)
                handlers.append(
                    _RotatingHandler(
                        path,
                        maxBytes=settings.max_bytes,
                        backupCount=settings.backup_count,
                        encoding="utf-8",
                    )
                )
            if not handlers:
                # 不启用任何输出时，避免标准库 lastResort 处理器意外写到终端。
                handlers.append(logging.NullHandler())
            for handler in handlers:
                handler.setFormatter(formatter)
                handler.setLevel(level)
        except BaseException:
            for handler in handlers:
                handler.close()
            raise
        logger = logging.getLogger("wintermute")
        previous_state = (
            _active_handle._previous_state
            if _active_handle is not None
            else (logger.level, logger.propagate, logger.disabled)
        )
        if _active_handle is not None:
            _active_handle._remove_handlers()
        handle = LoggingHandle(logger, tuple(handlers), previous_state)
        logger.setLevel(level)
        logger.propagate = False
        logger.disabled = False
        for handler in handlers:
            logger.addHandler(handler)
        _active_handle = handle
        return handle
