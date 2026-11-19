"""按配置创建主 Agent、子 Agent、上下文管理器和基础工具。"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from wintermute.agent import AgentRunner, Message, Model, Tool
from wintermute.agent.context import (
    ContextBudget,
    ContextManager,
    MainContextManager,
    ModelSummarizer,
    SubagentContextManager,
)
from wintermute.agent.tools import (
    BackgroundNotificationPublisher,
    BackgroundSubagentManager,
    SubagentConfig,
    create_edit_tool,
    create_exec_tool,
    create_read_tool,
    create_write_tool,
)
from wintermute.application.models import ModelSet
from wintermute.application.paths import _path
from wintermute.config.models import (
    AppConfig,
    ConfigError,
    ContextSettings,
    SubagentSettings,
    ToolsSettings,
)


def create_context_manager(
    settings: ContextSettings,
    model: Model,
    *,
    role: Literal["main", "subagent"] = "main",
) -> ContextManager | None:
    if not settings.enabled:
        return None
    manager = MainContextManager if role == "main" else SubagentContextManager
    return manager(
        ContextBudget(
            context_window=settings.context_window,
            reserve_tokens=settings.reserve_tokens,
            keep_recent_tokens=settings.keep_recent_tokens,
            max_summary_tokens=settings.max_summary_tokens,
        ),
        ModelSummarizer(
            model,
            input_limit=settings.summary_input_limit,
            timeout=settings.summary_timeout,
        ),
    )


def create_tools(settings: ToolsSettings, *, base_dir: str | Path = ".") -> tuple[Tool[Any], ...]:
    """仅构造显式启用的工具，工作目录以配置文件目录为基准。"""
    cwd = _path(settings.cwd, base_dir)
    tools: list[Tool[Any]] = []
    for name in settings.enabled:
        match name:
            case "read":
                tools.append(
                    create_read_tool(
                        cwd,
                        max_lines=settings.max_output_lines,
                        max_bytes=settings.max_output_bytes,
                    )
                )
            case "write":
                tools.append(create_write_tool(cwd))
            case "edit":
                tools.append(
                    create_edit_tool(
                        cwd,
                        max_lines=settings.max_output_lines,
                        max_bytes=settings.max_output_bytes,
                    )
                )
            case "exec":
                tools.append(
                    create_exec_tool(
                        cwd,
                        shell=settings.shell,
                        timeout=settings.command_timeout,
                        max_output_bytes=settings.max_output_bytes,
                        max_output_lines=settings.max_output_lines,
                    )
                )
            case _:
                raise ConfigError("工具名称不在内置工具列表中")
    return tuple(tools)


def create_runner(
    config: AppConfig,
    model: Model,
    *,
    base_dir: str | Path = ".",
    additional_tools: Sequence[Tool[Any]] = (),
    messages: Sequence[Message] = (),
) -> AgentRunner:
    """从快照创建独立主 Runner，自定义工具仍由应用显式注入。"""
    return AgentRunner(
        model,
        system_prompt=config.agent.system_prompt,
        tools=(*create_tools(config.tools, base_dir=base_dir), *additional_tools),
        messages=messages,
        max_turns=config.agent.max_turns,
        tool_timeout=config.agent.tool_timeout,
        model_timeout=config.agent.model_timeout,
        context_manager=create_context_manager(config.context, model),
    )


def create_subagent(
    settings: SubagentSettings, model: Model, *, base_dir: str | Path = "."
) -> SubagentConfig:
    """每次子任务创建独立压缩器，不共享主会话或其他子任务的摘要。"""

    def context_factory() -> ContextManager:
        manager = create_context_manager(settings.context, model, role="subagent")
        assert manager is not None
        return manager

    return SubagentConfig(
        settings.name,
        model,
        description=settings.description,
        system_prompt=settings.agent.system_prompt,
        tools=create_tools(settings.tools, base_dir=base_dir),
        max_turns=settings.agent.max_turns,
        tool_timeout=settings.agent.tool_timeout,
        model_timeout=settings.agent.model_timeout,
        timeout=settings.timeout,
        context_manager_factory=context_factory if settings.context.enabled else None,
    )


def create_background_manager(
    config: AppConfig,
    models: ModelSet,
    *,
    base_dir: str | Path = ".",
    notification_publisher: BackgroundNotificationPublisher | None = None,
) -> BackgroundSubagentManager:
    if not config.subagents:
        raise ConfigError("创建后台管理器需要至少一个子 Agent 配置")
    if any(settings.name not in models.subagents for settings in config.subagents):
        raise ConfigError("子 Agent 缺少对应模型实例")
    return BackgroundSubagentManager(
        [
            create_subagent(settings, models.subagents[settings.name], base_dir=base_dir)
            for settings in config.subagents
        ],
        max_concurrency=config.background.max_concurrency,
        max_tasks=config.background.max_tasks,
        max_output_bytes=config.background.max_output_bytes,
        notification_publisher=notification_publisher,
    )
