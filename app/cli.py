from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .app import WintermuteApp
from .config.config import Settings
from .llm.llm import OpenAICompatibleLLM
from .log.log import configure_logging
from .storage.storage import FileStore

logger = logging.getLogger(__name__)


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法:")

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法:")

    def error(self, message: str) -> None:
        translations = {
            "unrecognized arguments:": "无法识别的参数:",
            "expected one argument": "需要一个值",
        }
        for source, target in translations.items():
            message = message.replace(source, target)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 错误: {message}\n")


def build_app(settings: Settings) -> WintermuteApp:
    store = FileStore(settings.data_dir)
    llm = OpenAICompatibleLLM(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    return WintermuteApp(store, llm)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = ChineseArgumentParser(
        description="本地助手 MVP 命令行",
        add_help=False,
        usage="app [-h] [--data-dir 数据目录]",
    )
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示帮助信息并退出。")
    parser.add_argument("--data-dir", metavar="数据目录", type=Path, help="覆盖配置中的数据目录。")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if args.data_dir:
        settings = Settings(
            data_dir=args.data_dir,
            log_dir=settings.log_dir,
            log_retention_days=settings.log_retention_days,
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    log_path = configure_logging(settings.log_dir, retention_days=settings.log_retention_days)
    logger.info(
        "应用启动 data_dir=%s log_path=%s model=%s",
        settings.data_dir,
        log_path,
        settings.model,
    )

    app = build_app(settings)
    app.bootstrap()
    print("应用已启动。输入 /exit 退出。")

    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.info("应用退出")
            print()
            return 0

        if not text:
            continue
        if text in {"/exit", "/quit"}:
            logger.info("应用退出")
            return 0

        result = app.handle_user_input(text)
        print(result.response)


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
