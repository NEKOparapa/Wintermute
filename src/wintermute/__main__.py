"""通过 JSON 配置启动 Telegram、HTTP 和共享交流运行层。"""

import argparse
import asyncio
import signal
from pathlib import Path

from wintermute.config import ConfigError, ConfigManager
from wintermute.config.factory import configure_app_logging, create_store, open_models
from wintermute.interfaces.service import ConversationService
from wintermute.logging import get_logger


async def serve(path: Path) -> None:
    manager = ConfigManager(path)
    config = await asyncio.to_thread(manager.load)
    if not config.interfaces.http.enabled and not config.interfaces.telegram.enabled:
        raise ConfigError("请在 JSON 中启用 interfaces.http 或 interfaces.telegram")
    with configure_app_logging(config, base_dir=manager.base_dir):
        try:
            with create_store(config.events, base_dir=manager.base_dir) as store:
                async with open_models(config) as models:
                    async with ConversationService(
                        config, models, store, base_dir=manager.base_dir
                    ) as service:
                        stop = asyncio.Event()
                        loop = asyncio.get_running_loop()
                        installed = False
                        stopped = asyncio.create_task(stop.wait())
                        monitored = asyncio.create_task(service.wait())
                        try:
                            # Windows 由 asyncio.run 处理 Ctrl+C；支持的平台另处理 SIGTERM。
                            try:
                                loop.add_signal_handler(signal.SIGTERM, stop.set)
                                installed = True
                            except NotImplementedError:
                                pass
                            done, _ = await asyncio.wait(
                                (stopped, monitored), return_when=asyncio.FIRST_COMPLETED
                            )
                            if monitored in done:
                                await monitored
                        finally:
                            if installed:
                                loop.remove_signal_handler(signal.SIGTERM)
                            stopped.cancel()
                            monitored.cancel()
                            await asyncio.gather(stopped, monitored, return_exceptions=True)
        except Exception as exc:
            get_logger("service").error("交流服务退出", extra={"error_type": type(exc).__name__})
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/settings.json"))
    arguments = parser.parse_args()
    try:
        asyncio.run(serve(arguments.config))
    except KeyboardInterrupt:
        pass
    except ImportError:
        parser.exit(1, "请先运行 uv sync --extra openai --extra interfaces 安装可选依赖。\n")
    except ConfigError as exc:
        parser.exit(1, f"配置错误：{exc}\n")
    except Exception as exc:
        # 网络异常有时包含请求地址与凭据，命令行仅显示错误类型。
        parser.exit(1, f"服务启动或运行失败：{type(exc).__name__}，请检查配置和日志。\n")


if __name__ == "__main__":
    main()
