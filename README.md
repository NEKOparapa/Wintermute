# Wintermute

一个逐渐理解你的个人 AI 执事。项目以可复用的 `AgentRunner` 和分层事件运行层为基础，后续继续完善长期记忆、人格和更多终端。

当前已实现 Python 异步 Agent 核心、文件与命令基础工具、前台和后台子 Agent、主/子 Agent 上下文管理、支持多终端和 SQLite 持久化的三层事件运行、可配置的 Telegram 与 HTTP 交流接口、JSON 配置与结构化日志，以及 OpenAI 兼容 Responses / Chat Completions 适配器。默认使用 **Responses API**，保留异步流式输出和工具调用。项目由 [uv](https://docs.astral.sh/uv/) 管理，要求 **Python >= 3.12**，默认开发版本为 3.12。

## 快速开始

```bash
uv sync --locked
uv run python examples/mock_agent.py
```

本地示例无需 API Key，会完成「模型调用 add → 校验参数 → 执行工具 → 工具结果回传 → 模型回答」两轮流程。

## JSON 配置

`wintermute.config` 专门管理配置数据和 JSON 文件；`wintermute.config.factory` 将配置创建为模型、工具、主/子 Agent、日志和事件运行组件。没有导入即创建客户端或启动任务的行为。

从模板创建本地配置，填好环境变量后运行：

```powershell
Copy-Item config/settings.example.json config/settings.json
$env:ARK_API_KEY = "你的 API Key"
uv run --extra openai python examples/configured_agent.py "读取 README.md，介绍项目结构。"
```

macOS / Linux：

```bash
cp config/settings.example.json config/settings.json
export ARK_API_KEY="你的 API Key"
uv run --extra openai python examples/configured_agent.py "读取 README.md，介绍项目结构。"
```

`config/settings.json` 是被 Git 忽略的本地配置；模板不包含凭据。默认使用豆包 Responses 接口，启用 `read` 工具和一个独立的 `reader` 子角色。示例通过交流入口提交输入，保存会话历史；后台结果通过通知入口触发主 Agent，退出前等待已启动的任务和通知处理完成。可用 `--conversation other-session` 新建独立会话，用 `--config /绝对路径/settings.json` 指定其他配置。同一个数据库中其他本地会话的待处理事件也会恢复；每个会话独立管理后台任务，结果通知返回原会话。

所有配置中的相对资源路径都以 **JSON 文件所在目录** 为基准。例如模板中的 `tools.cwd: ".."` 指向项目根目录，数据库和日志分别写入项目的 `data/`、`logs/`。应用创建组件时须传入 `base_dir=manager.base_dir`，与示例保持一致。`--config` 参数本身按启动命令的工作目录解析。

| 配置分组 | 主要内容 |
| --- | --- |
| `schema_version` | 当前为 `1` |
| `model` | `api`（默认 `responses`，可选 `chat_completions`）、服务地址、模型、凭据、请求超时、重试和 `extra_body` |
| `agent` | 系统提示词、模型轮数上限、模型及工具超时 |
| `context` | 是否启用主 Agent 自动摘要、上下文窗口及各项预算、摘要超时 |
| `tools` | 显式启用的 `read/write/edit/exec`、工作目录、命令超时、输出限制 |
| `subagents` | 每个角色的名称、说明，以及独立的 `agent/tools/context`；`model: null` 继承主模型 |
| `background` | 后台任务并发数、任务容量和结果大小限制 |
| `events` | SQLite 路径、事件大小、调度并发、租约、投递重试及资料组装预算 |
| `logging` | 日志级别、控制台、`text/json` 格式、文件路径和轮转策略 |
| `interfaces` | HTTP 与 Telegram 开关、凭据、终端与会话绑定，以及监听或轮询参数 |

未填写的字段使用模型默认值；模板展示了一组可直接使用的设置。主/子 Agent 的上下文管理分别创建，摘要状态不共享。自定义 Python 工具仍通过 `create_runner(..., additional_tools=...)` 注入；子 Agent 工具通过后台管理器的 `tools` 接入。配置文件不承载可执行 Python 代码。

```python
from wintermute.config import ConfigManager

manager = ConfigManager("config/settings.json")
config = manager.load()
print(config.redacted_dict())  # 展示配置时遮盖凭据。

# 对象递归合并，列表整体替换；校验通过后原子保存并更新内存快照。
config = manager.update({"agent": {"max_turns": 12}, "logging": {"level": "DEBUG"}})
manager.save(config)
config = manager.reload()  # 重新读取磁盘；不会热修改已经创建的运行对象。
```

加载会拒绝未知字段、重复 JSON 键、NaN/Infinity 和无效预算。保存采用同目录临时文件加原子替换；失败不会覆盖原有有效配置。一个管理器内的更新受锁保护，多个进程之间不提供补丁合并。若在异步请求中读写配置，可通过 `await asyncio.to_thread(manager.load)` 等方式调用同步文件 API。

建议使用 `api_key_env` 指定环境变量。若填写明文 `api_key`，其优先级更高，`save()` 会原样保存该值以支持读写往返；用于展示的 `redacted_dict()` 不应拿来保存。其他环境变量不会自动覆盖 JSON，也不会自动加载 `.env`。

无需密钥的完整离线演示：

```bash
uv run python examples/configuration.py
```

该示例在临时目录中保存、更新和重载 JSON，创建 SQLite 调度器运行连续两轮对话，并验证结构化日志与关联字段。

## 日志

`wintermute.logging` 基于标准库 `logging`，支持控制台和 UTF-8 轮转文件、文本或逐行 JSON 格式、级别控制和异步任务关联字段。仅显式配置时安装本模块处理器或创建日志文件；只管理 `wintermute` 命名空间，保留应用根 logger 和第三方 logger 的配置。重复配置不会叠加本模块的输出处理器。

```python
from wintermute.config import ConfigManager
from wintermute.config.factory import configure_app_logging
from wintermute.logging import get_logger, log_context

manager = ConfigManager("config/settings.json")
config = manager.load()
logger = get_logger("my_application")

with configure_app_logging(config, base_dir=manager.base_dir):
    with log_context(conversation_id="work", terminal_id="desktop"):
        logger.info("接收输入", extra={"layer": "conversation"})
```

`configure_app_logging()` 会收集主/子模型、HTTP 与 Telegram 配置中的凭据及其指定的环境变量值，用于日志脱敏。仅需独立日志模块时可调用 `configure_logging(config.logging, base_dir=..., secrets=...)`，返回的句柄支持 `with` 或显式 `close()`。常见敏感字段、已注册凭据和异常文本均经过脱敏处理。

JSON 日志包含 `timestamp`、`level`、`logger`、`message`，以及按需加入的 `context`、`extra`、`exception`。`log_context()` 基于 `ContextVar`，可跨 `await` 保留字段；运行层记录任务、会话和子任务关联信息。默认运行日志记录生命周期和元数据，不记录聊天正文、工具参数或工具结果。业务代码自行写日志时也应遵循这一约定。

`logging.file` 为 `null` 时不写文件；`console: false` 可关闭控制台输出。文件达到 `max_bytes` 后轮转，最多保留 `backup_count` 个历史文件；模板默认每份 5 MiB，保留 3 份。`backup_count: 0` 沿用标准库行为，不轮转，文件持续追加。

## Telegram 与 HTTP 交流接口

两个接口都将文本提交到 `EventGateway.conversation`，经过持久化、会话调度和 `AgentRunner` 处理。Telegram 自动回发完整回答；HTTP 客户端查询任务和领取回复。后台子 Agent 的完成通知继续走统一通知入口，处理后的回答返回启动该任务的终端。

`settings.example.json` 默认关闭网络接口。`config/interfaces.example.json` 是完整的服务配置模板，默认启用本机 HTTP，预留关闭状态的 Telegram 示例绑定。首次创建服务配置：

```powershell
Copy-Item config/interfaces.example.json config/settings.json
$env:ARK_API_KEY = "你的模型 API Key"
$env:WINTERMUTE_HTTP_TOKEN = "替换为独立随机口令"
uv run --extra openai --extra interfaces python -m wintermute --config config/settings.json
```

已有本地配置时，将模板中的 `interfaces` 分组合并进去即可。启动命令会持续运行，按 `Ctrl+C` 关闭；修改配置后重启服务生效。`interfaces` 可选依赖包含 `aiohttp`，仅使用 Agent 核心不需要安装。HTTP 默认监听 `127.0.0.1:8080`，可通过 `host`、`port` 和 `max_body_bytes` 调整。

每个 HTTP 终端在 `interfaces.http.terminals` 中配置一项：

```json
{
  "terminal_id": "http-desktop",
  "user_id": "local",
  "conversation_id": "main",
  "token_env": "WINTERMUTE_HTTP_TOKEN"
}
```

`terminal_id` 是终端标识，`user_id` 是应用内的用户归属，`conversation_id` 是持久会话。另一个 HTTP 或 Telegram 终端使用相同的 `user_id`、`conversation_id` 即可接续聊天，`terminal_id` 必须不同。不同会话各自保留上下文和后台任务；同一会话的消息串行处理。可以增加多个终端绑定，并为 HTTP 终端分别指定凭据环境变量。

HTTP 每个业务请求均携带 `X-Terminal-ID` 和 `Authorization: Bearer ...`。用户及会话归属由服务端配置决定，请求体只包含消息内容和消息 ID。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /health` | 无需认证，返回 `{"status":"ok"}` |
| `POST /v1/messages` | 提交 `{"message_id":"...","text":"..."}`；返回 HTTP 202 和 `event_id`、`job_id`、`duplicate`，无需等待模型完成 |
| `GET /v1/jobs/{job_id}` | 查询授权范围内的任务；返回 `status`、`error` 和 `outputs` 等字段 |
| `GET /v1/outputs?limit=100` | 返回 `{"outputs":[...]}`，包含当前终端、当前会话尚未确认的普通回复及后台结果通知 |
| `POST /v1/outputs/{output_id}/ack` | 确认终端已收到指定输出；重复确认安全，返回 `status: "delivered"` |

请求重试时复用原 `message_id`，相同消息不会重复执行；同一 ID 改成其他内容会返回 409。新消息使用新的 ID。缺少或错误的认证返回 401，其他终端无权读取的任务或输出返回 404。GET 查询不会确认输出，客户端展示或保存完成后再调用 `ack`；客户端也可按 `output_id` 去重。

在另一个已设置相同 `WINTERMUTE_HTTP_TOKEN` 的 PowerShell 终端中发送消息：

```powershell
$headers = @{
    "X-Terminal-ID" = "http-desktop"
    "Authorization" = "Bearer $env:WINTERMUTE_HTTP_TOKEN"
}
$body = @{
    message_id = [guid]::NewGuid().ToString("N")
    text = "你好，请介绍一下你能做什么。"
} | ConvertTo-Json
$receipt = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/messages" `
    -Headers $headers -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/jobs/$($receipt.job_id)" -Headers $headers

# 模型处理完成后领取回复；没有输出时可稍后再次查询。
$inbox = Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/outputs?limit=100" -Headers $headers
foreach ($output in $inbox.outputs) {
    Write-Output $output.text
    Invoke-RestMethod -Method Post `
        -Uri "http://127.0.0.1:8080/v1/outputs/$($output.output_id)/ack" -Headers $headers
}
```

macOS / Linux 的 `curl` 提交示例：

```bash
curl -sS http://127.0.0.1:8080/v1/messages \
  -H "X-Terminal-ID: http-desktop" \
  -H "Authorization: Bearer $WINTERMUTE_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"message_id":"demo-001","text":"你好，请介绍一下你能做什么。"}'

curl -sS 'http://127.0.0.1:8080/v1/outputs?limit=100' \
  -H "X-Terminal-ID: http-desktop" \
  -H "Authorization: Bearer $WINTERMUTE_HTTP_TOKEN"
```

HTTP 第一版通过轮询获取最终输出；底层 Runner 的异步流式事件仍可供 Python 订阅器使用，HTTP 暂不提供 SSE。

启用 Telegram 时，将 `interfaces.telegram.enabled` 改为 `true`，设置 `TELEGRAM_BOT_TOKEN`，再将模板中的示例 ID 替换为实际聊天与用户 ID：

```json
{
  "terminal_id": "tg-phone",
  "user_id": "local",
  "conversation_id": "main",
  "chat_id": 123456789,
  "allowed_user_ids": [123456789]
}
```

`chat_id` 指定允许接入的私聊或群聊，`allowed_user_ids` 限定该聊天中可以向 Agent 发消息的 Telegram 用户；两项都需要匹配。私聊的两个 ID 通常相同，群聊使用群的 `chat_id` 和允许成员的用户 ID。与前面的 HTTP 绑定一样使用 `local/main` 时，可在桌面 HTTP 和手机 Telegram 之间继续同一段对话。新增聊天时增加绑定项，每个 `chat_id` 只能绑定一次。

Telegram 通过 [Bot API](https://core.telegram.org/bots/api) 的 `getUpdates` 长轮询接收消息，不需要配置公网 webhook；如果该 Bot 已使用 webhook，需要先自行切换到轮询模式，服务不会自动删除 webhook 或丢弃待处理更新。`poll_timeout` 控制长轮询时长，`request_timeout` 必须更大；`retry_delay` 控制失败后的重试间隔，`api_base` 可指定 Bot API 服务地址。

当前接收白名单内用户的普通文本消息，包括配置群内的文本；不处理机器人代发、匿名群管理员代发、编辑消息或附件。回复通过 `sendMessage` 发送普通文本，长回答自动拆分。接收端以更新 ID 去重；发送失败会重试，但发送成功后确认丢失或进程中断仍可能产生重复回复，Telegram 输出不承诺严格只发送一次。HTTP 与 Telegram 接入不会启动音视频持续处理。

接口实现使用 [aiohttp](https://docs.aiohttp.org/en/stable/web_quickstart.html)。日志继续使用 `logging` 分组配置，模板写入 `logs/wintermute.log`；接口认证口令和 Bot Token 也纳入日志脱敏。

嵌入 Python 应用时，可从 `wintermute.interfaces.service` 导入 `ConversationService`，使用 `async with ConversationService(config, models, store, base_dir=manager.base_dir) as service` 启动和关闭接口、调度器及后台任务。上下文内可 `await service.wait()` 持续运行并接收调度器故障，避免运行层停止后入口仍继续接收消息。`models` 来自 `open_models()`，模型客户端、日志和 `SQLiteStore` 的生命周期由外层应用管理。

## 连接豆包 / OpenAI 兼容服务

```bash
uv sync --locked --extra openai
```

Windows PowerShell：

```powershell
$env:ARK_API_KEY = "你的 API Key"
$env:ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
$env:ARK_MODEL = "doubao-seed-2-0-lite-260428"
uv run --extra openai python examples/ark_agent.py
```

macOS / Linux：

```bash
export ARK_API_KEY="你的 API Key"
export ARK_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export ARK_MODEL="doubao-seed-2-0-lite-260428"
uv run --extra openai python examples/ark_agent.py
```

示例通过环境变量读取配置，不自动加载 `.env`。服务地址和模型有上述默认值；Key 必须自行传入。两个豆包示例默认通过 `/responses` 请求模型，关闭 thinking，验证异步文本流和工具调用。其他支持 Responses 的服务可自行构造 `AsyncOpenAI(base_url=..., api_key=...)`，再注入 `OpenAIModel(client, model)`；客户端由调用方管理和关闭。

```python
from openai import AsyncOpenAI
from wintermute.agent.providers import OpenAIModel, OpenAIChatModel

# 在 async with AsyncOpenAI(...) as client 中创建适配器。
model = OpenAIModel(client, model="doubao-seed-2-0-lite-260428")

# 仅支持 Chat Completions 的服务可显式使用现有适配器。
chat_model = OpenAIChatModel(client, model="your-model")
```

`OpenAIModel` 是 `OpenAIResponsesModel` 的默认别名，也可直接导入后者。Responses 默认设置 `store=False`，每轮把 AgentRunner 提供的历史上下文转换为 `input` 发送，不使用 `previous_response_id` 隐式串联服务端状态。工具调用和结果通过 `call_id` 配对；Responses 原始输出项随消息保留，供后续请求回传，其中推理元数据不作为用户可见文本流输出。Chat Completions 适配器继续使用 `messages` 和 `tool_call_id`。

若在 `transform_context` 中改写 assistant 消息的正文或工具调用，请同时清空该消息的 `provider_data`；适配器会拒绝与正文不一致的旧原始输出，避免改写被悄悄忽略。仅裁剪完整轮次时可保留剩余消息的元数据。

接口格式参考 [OpenAI Responses 文档](https://developers.openai.com/api/docs/guides/migrate-to-responses)及[流式事件文档](https://developers.openai.com/api/reference/resources/responses/streaming-events)。当前两个适配器处理文本和 function tools，多模态和服务端内置工具暂未接入；第三方服务应支持所选接口及模型。

## 使用 AgentRunner

```python
from pydantic import BaseModel, ConfigDict
from wintermute.agent import AgentRunner, Tool, ToolContext, ToolResult


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int


async def add(params: AddInput, context: ToolContext) -> ToolResult:
    return ToolResult(str(params.a + params.b))


# model 实现 Model.stream(context)，或使用默认的 OpenAIModel。
runner = AgentRunner(
    model,
    system_prompt="你是 Wintermute，请按需使用工具。",
    tools=[Tool("add", "两个整数相加", AddInput, add)],
    max_turns=20,
    model_timeout=120,
    tool_timeout=30,
)

# 在异步函数中调用：
result = await runner.run("请用工具计算 19 + 23")
print(result.response.text)
```

| 接口 | 行为 |
| --- | --- |
| `await run(prompt)` | 添加用户消息，执行到最终回答；返回本次新增消息、最终回答和模型调用轮数 |
| `await run(prompt, references=..., record_history=False)` | 传入本轮资料，并以临时历史运行；成功、错误或取消后恢复原交流历史，执行消息仍由结果与事件提供 |
| `await continue_run()` | 从 user/tool 尾部重试，或消费已排队的输入 |
| `subscribe(listener)` | 订阅同步/异步生命周期事件；返回取消订阅函数 |
| `steer(message)` | 在当前模型响应及整批工具完成后，优先注入所有 steering 消息 |
| `follow_up(message)` | 当前工具循环自然结束后，依次注入后续消息，每次一条 |
| `cancel()` | 请求取消，运行协程抛出 `asyncio.CancelledError` |
| `await wait_until_idle()` | 等待结束和清理，不重新抛出运行错误；取消等待者不会取消运行 |
| `state` | 获取与内部状态隔离的历史、运行状态、待执行工具和队列数量快照 |
| `context_state` | 获取摘要、已摘要消息数、压缩次数和最近一次准备后的输入估算；未配置管理器时为 `None` |
| `reset()` | 空闲时清空历史、队列、错误和上下文摘要，保留模型、工具和订阅 |
| `restore_messages(messages)` | 空闲且没有排队输入时，校验并恢复持久化历史；摘要缓存由管理器按历史前缀检查 |

同一 runner 只支持一个活动运行，并在同一个事件循环中使用。活动期间再次运行或 reset 会抛 `AgentBusyError`；跨会话请创建不同实例。

`references` 接受 `UserMessage` 序列，与普通对话历史分开保留。Runner 在压缩完成后将其标为外部资料并展开到模型输入；它们只对本次 `run()` 有效，不进入历史摘要。`record_history` 默认是 `True`，可独立于 `references` 使用；通知等临时运行设为 `False` 时，也不会消费原有的 steering / follow-up 队列。

## 上下文长度与自动摘要

向主 Runner 传入 `MainContextManager`，向子角色传入创建 `SubagentContextManager` 的工厂，即可分别控制主/子 Agent 的预算和摘要方式。两者当前使用增量摘要算法，具有不同的角色入口和独立状态；以后可以分别替换管理器、摘要器或计数器。未配置管理器时，Runner 沿用原有的完整历史发送行为。

```python
from wintermute.agent import AgentRunner
from wintermute.agent.context import (
    ContextBudget,
    MainContextManager,
    ModelSummarizer,
    SubagentContextManager,
)
from wintermute.agent.tools import SubagentConfig, create_read_tool, create_subagent_tool

# model 用于任务执行，summary_model 可使用专门负责摘要的模型。
main_context = MainContextManager(
    ContextBudget(
        context_window=64_000,
        reserve_tokens=4_096,
        keep_recent_tokens=8_192,
        max_summary_tokens=2_048,
    ),
    ModelSummarizer(summary_model, input_limit=16_000),
)


def make_child_context() -> SubagentContextManager:
    return SubagentContextManager(
        ContextBudget(
            context_window=16_000,
            reserve_tokens=2_048,
            keep_recent_tokens=2_048,
            max_summary_tokens=1_024,
        ),
        ModelSummarizer(summary_model, input_limit=12_000),
    )


subagent = create_subagent_tool(
    [
        SubagentConfig(
            "reader",
            model,
            system_prompt="阅读指定文件，保留任务目标与关键路径。",
            tools=[create_read_tool(".")],
            context_manager_factory=make_child_context,
        )
    ]
)
runner = AgentRunner(model, tools=[subagent], context_manager=main_context)
```

`ContextBudget` 的输入上限为 `context_window - reserve_tokens`。窗口大小由应用明确配置，不自动推断模型容量；`reserve_tokens` 为后续输出预留空间，使用模型适配器时还应将供应商的 `max_output_tokens` 配置在该预留范围内。`keep_recent_tokens` 决定压缩后保留多少近期原始内容，`max_summary_tokens` 限制摘要大小。

每次模型请求前，Runner 先执行已有的 `transform_context`，再交给上下文管理器。管理器将系统提示词、工具定义和消息一起计入预算；超限时，将旧消息前缀与上一版摘要合并，保留近期原文。assistant 工具调用及其全部结果作为一个完整单元保留或摘要，不会产生孤立的工具结果。最新的消息单元必须保留，因此超长用户输入、必要工具结果或系统/工具定义本身超出预算时，会抛出 `ContextLimitError`，不会静默截掉请求。

`runner.state.messages` 始终保留原始完整历史，压缩只改变发送给模型的上下文。`runner.context_state` 可观察 `summary`、`summarized_messages`、`compactions` 和 `last_input_tokens`。后续压缩使用旧摘要与新增长历史增量更新；已摘要前缀被变换或改写时，缓存失效。取消、摘要失败或压缩后仍不满足预算时，不提交新的摘要状态；`reset()` 会同时清除上下文管理状态。

默认 `EstimatedTokenCounter` 使用 UTF-8 字节数及结构开销作保守估算，也计入保留消息的 `provider_data`。它不是供应商的精确 tokenizer，因而本地预算保证以该计数器结果为准；需要模型专用计数时，可实现 `TokenCounter.count(context)` 和 `count_text(text)` 后传给管理器及摘要器。当前不读取供应商 usage 来校准，也不在服务商返回超窗错误后自动重试。

`ModelSummarizer` 通过 `Model.stream()` 调用摘要模型，不注册工具，不将摘要过程的文本增量混入主 Agent 回复。摘要输入序列化为资料文本，省略推理元数据；工具结果默认最多保留 2000 字符并注明截断，可用 `max_tool_result_chars` 调整。它按独立的 `input_limit` 分块摘要，默认最多处理 64 个块；摘要为空、截断、尝试调用工具或超过输出预算时抛出 `SummaryError`。`timeout` 默认是每次摘要请求 60 秒。Runner 的 `model_timeout` 覆盖整个上下文准备与正常模型调用，可能包含多次摘要请求，配置时应为摘要留出时间。

可替换的三个接口分别负责不同的行为：

| 接口 | 负责内容 |
| --- | --- |
| `ContextManager.prepare(context) -> ManagedContext`、`reset()`、`state` | 选择保留哪些消息、何时压缩及如何维护状态 |
| `Summarizer.summarize(SummaryRequest) -> str` | 将 `messages` 与 `previous_summary` 合并为摘要，遵守 `max_tokens`，可根据 `role` 区分主/子任务 |
| `TokenCounter.count(context)`、`count_text(text)` | 估算或精确计算完整上下文和文本长度 |

主 Runner 的管理器只属于该会话；子角色的 `context_manager_factory` 会在每个实际前台或后台子任务启动时调用，注册工具时不会调用。工厂必须创建新实例，不能让不同子任务共享有状态的管理器；同一管理器也拒绝并发 `prepare()`。模型和自定义摘要器若被多个任务复用，仍需支持并发。上下文状态目前保存在内存中。

自定义上下文管理器需要原样保留 `AgentContext.reference_messages`，并按 `context.for_model()` 展开后的实际模型输入计数，避免遗漏参考资料及其标记开销。内置管理器和默认计数器已处理这一点；直接实现 `Model.stream()` 时仍读取 `context.messages`，无需额外拼接参考资料。

配置管理器后，每轮会产生 `context_prepared` 事件，包含 `context_tokens`、`context_limit` 和 `compacted`。子任务的同类事件通过已有的 `on_event` 转发。无需 Key 的示例使用模拟摘要器，展示主 Agent 多轮压缩、真实调用 `subagent` 工具，以及两个子任务分别压缩且互不污染：

```bash
uv run python examples/context_management.py
```

## 文件与命令基础工具

通过统一工厂接入现有 Runner：

```python
from wintermute.agent import AgentRunner
from wintermute.agent.tools import create_builtin_tools

runner = AgentRunner(
    model,
    tools=create_builtin_tools(
        cwd=".",
        command_timeout=60,
        max_output_bytes=50 * 1024,
        max_output_lines=2000,
    ),
)
result = await runner.run("读取 README.md，检查示例是否完整。")
```

| 工具 | 模型调用参数 | 行为 |
| --- | --- | --- |
| `read` | `path`、`offset`（默认 1）、`limit`（可选） | 读取 UTF-8 文本；按行分页，保留输出头部，并提示下一次读取位置 |
| `write` | `path`、`content` | 创建或完整覆盖文件，自动创建父目录，使用原子替换落盘 |
| `edit` | `path`、`edits: [{old_text, new_text}]` | 每项基于原文件唯一匹配；拒绝重叠，全部验证成功后一次写入，返回 unified diff |
| `exec` | `command`、`timeout`（可覆盖默认秒数） | 在固定工作目录执行前台命令，合并 stdout/stderr，返回退出码和输出尾部 |

`read`、`edit` 和 `exec` 的正文输出默认限制为 2000 行 / 50 KiB，额外附加简短状态或截断提示。首行过长的文件会提示改用命令按范围检查；不会把半行伪装成完整读取结果。`read` 不处理图片和二进制文件。`edit` 支持 UTF-8 BOM 和 CRLF 换行，单个原文件上限为 10 MiB，采用精确文本匹配，不进行模糊替换。

文件路径支持相对 `cwd`、绝对路径和 `~`。文件 I/O 在线程中运行；同一事件循环里的 write/edit 对同一路径串行执行，取消会等待已经开始的文件操作结束后才释放锁。外部进程的修改不在该锁的管理范围内。

`exec` 在 Windows 默认选择 PowerShell（优先 `pwsh`），在 Linux/macOS 选择 Bash 或 sh，也可以向工厂传入 `shell` 指定支持的 shell 可执行文件。工具说明会告知模型实际 shell。每次调用使用新的 shell，`cd` 和临时环境修改不会跨调用保留。非零退出、超时返回错误结果；结束、超时或取消时，Windows 通过 Job Object 清理子进程，POSIX 清理整个进程组。显式创建新会话的 POSIX 后台进程不属于该进程组，此工具用于前台任务。超过输出上限的部分不在内存中累计，需要完整日志时可在命令中重定向到文件。

这些工具使用当前进程的本机权限；`cwd` 是路径解析基准，不是访问沙箱。Runner 只执行显式注册的工具。需要更小的工具集时，可分别使用 `create_read_tool`、`create_write_tool`、`create_edit_tool`、`create_exec_tool` 构造后传入 `tools`。

无需 Key 的完整示例会在临时目录中真实执行四种工具：

```bash
uv run python examples/builtin_tools.py
```

使用豆包执行指定任务（环境变量与前述示例相同）：

```bash
uv run --extra openai python examples/ark_tools.py --cwd . "读取 README.md，概括项目的公共接口。"
```

## 子 Agent 工具

`create_subagent_tool` 将角色配置注册为一个前台工具，直接放进父 `AgentRunner` 的 `tools`；父 Agent 等整批子任务完成后继续处理结果。每个子任务创建新的 `AgentRunner`，具有独立消息历史，只使用指定的模型、系统提示词和工具；父对话历史、提示词和工具不会自动继承。需要的上下文应写入任务描述。

```python
from wintermute.agent import AgentRunner
from wintermute.agent.tools import (
    SubagentConfig,
    create_builtin_tools,
    create_read_tool,
    create_subagent_tool,
)

# model 可以使用支持并发请求的 OpenAIModel，也可以为每个角色指定不同模型。
subagent = create_subagent_tool(
    [
        SubagentConfig(
            "scout",
            model,
            description="阅读项目文件并整理事实",
            system_prompt="你是代码调查员，只阅读文件，返回相关路径和发现。",
            tools=[create_read_tool(".")],
            max_turns=10,
            model_timeout=60,
            tool_timeout=10,
            timeout=120,
        ),
        SubagentConfig(
            "planner",
            model,
            description="根据任务提供实施计划",
            system_prompt="你是规划员，根据收到的信息给出简短的实施步骤。",
            tools=[],
            timeout=120,
        ),
    ],
    max_concurrency=4,
    max_tasks=8,
)
runner = AgentRunner(
    model,
    tools=[*create_builtin_tools("."), subagent],
    system_prompt="按需委派独立任务，收到结果后综合回答。",
)
result = await runner.run("并行请 scout 阅读 README.md、planner 列出发布前的检查步骤。")
```

模型可以发起单个任务，或一次提交多个并行任务，两种输入互斥：

```json
{"agent": "scout", "task": "读取 README.md，概括公共接口。"}
```

```json
{"tasks": [{"agent": "scout", "task": "读取 README.md。"}, {"agent": "planner", "task": "列出发布前的检查步骤。"}]}
```

默认每次最多 8 个任务、同时执行 4 个；同一个工具实例在同一事件循环中的多次调用共享并发额度。任务完成顺序可以不同，返回的 JSON `results` 始终按输入顺序排列，每项包含 `agent`、`task`、`status`、`output`、`turns` 和 `truncated`。失败项额外带有 `error`，其余子任务继续执行；任意一项失败会使外层工具结果的 `is_error=True`。未知角色或任务数量超限会在启动任何子任务前拒绝整批请求。每项输出默认最多保留 50 KiB，可通过 `max_output_bytes` 调整。

子配置中的 `model_timeout`、`tool_timeout` 和 `max_turns` 沿用 `AgentRunner` 的语义；`timeout` 限制子任务开始运行后的总时间，不包含等待并发额度的时间。父 Runner 的 `tool_timeout` 则覆盖整个批次，包括排队时间，应为子任务留出足够时间。

调用父 Runner 的 `cancel()`、取消 `run()` 的调用任务，或触发父工具超时，都会取消正在执行和排队的子任务，并等待它们清理完成。主动取消时，调用方仍需等待原运行协程，或调用 `await runner.wait_until_idle()`；原运行协程会收到 `asyncio.CancelledError`。父工具超时则生成错误工具结果，交给父模型继续处理。已经发生的文件或命令副作用不会回滚。

可向工厂传入 `on_event` 同步或异步回调，接收 `SubagentEvent`：`call_id` 是父工具调用 ID，`task_index` 是从 0 开始的任务索引，`agent` 和 `task` 标识子任务，`event` 是原始 `AgentEvent`。文本增量、工具调用和结束状态可以用于显示并行进度；回调的普通异常会被记录并隔离。父历史只接收汇总工具结果，不会追加子任务的完整历史。

这一版使用同一进程中的 `asyncio` 任务，不提供文件系统或进程隔离、持久会话与串行链。模型和工具对象由配置注入，复用时需要支持并发；消息历史由各 Runner 单独维护。子工具集不会自动加入 `subagent`。`create_builtin_tools` 仍返回四个文件与命令工具，子 Agent 工具需要显式注册。

无需 Key 的示例会并行启动两个子 Agent，其中一个通过真实 `read` 工具读取临时文件，另一个只执行规划，最后由父 Agent 汇总：

```bash
uv run python examples/subagents.py
```

## 后台子 Agent

`BackgroundSubagentManager` 让子任务在后台运行，启动工具立即返回任务 ID。下面展示原有的直接 Runner 模式：主 Agent 可以继续对话；子任务完成、失败或取消后，管理器自动投递结果。主 Agent 正忙时，结果先进入队列；空闲后，管理器自动调用 `run()` 开始新一轮。接入多终端和持久化事件运行层时，使用下一节的 `notification_publisher` 模式，由统一调度器处理通知。

```python
from wintermute.agent import AgentRunner
from wintermute.agent.tools import BackgroundSubagentManager, SubagentConfig, create_read_tool

background = BackgroundSubagentManager(
    [
        SubagentConfig(
            "reader",
            model,
            description="读取文件并返回摘要",
            system_prompt="只阅读指定资料，返回事实摘要。",
            tools=[create_read_tool(".")],
            timeout=120,
        )
    ],
    max_concurrency=4,
)
runner = AgentRunner(
    model,
    tools=background.tools,
    system_prompt="按需启动后台任务，收到完成通知后向用户报告结果。",
)
background.bind(runner)

# 在异步函数中使用；真实应用通常把整个对话服务放在该生命周期内。
async with background:
    first = await background.send("在后台读取 README.md 并总结公共接口。")
    print(first.response.text)
    second = await background.send("等待期间，解释一下你支持哪些工具。")
    print(second.response.text)
    await background.wait_until_idle()
```

用户输入建议统一通过 `await background.send(...)` 提交，与完成通知共用串行队列，避免自动通知恰好占用主 Runner 时再次 `run()` 导致 `AgentBusyError`。子任务仍可并行运行，主模型一次处理一条消息。取消 `send()` 的等待者不会撤回已经入队的消息；中断当前主回复应使用 `runner.cancel()`。通过 `runner.subscribe(...)` 观察主 Agent 事件，可以同时展示用户对话回复和自动通知触发的新回复；自动唤醒不会改变既有 `send()` 返回的结果。

管理器提供三个模型工具：

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `subagent_start` | `{agent, task}` 或 `{tasks: [{agent, task}, ...]}` | 最多 8 项一批，整批预检后立即返回 `tasks` 数组，各项含 `task_id`、`agent`、`status` |
| `subagent_status` | `{task_id}` 或 `{}` | 查询单个任务的 `task` 状态快照，或列出 `tasks` 中的全部保留记录 |
| `subagent_cancel` | `{task_id}` | 请求取消，返回 `cancel_requested` 和当前 `task` 状态；清理结束后状态变为 `cancelled` |

任务状态依次为 `queued`、`running`，终态为 `completed`、`error` 或 `cancelled`；请求取消但尚未完成清理时为 `cancelling`。状态快照还包含任务描述、最终输出、错误、模型轮数、输出截断标志，以及独立的通知状态。通知状态包括 `none`、`pending`、`processing`、`published`、`delivered`、`failed` 和 `suppressed`；直接 Runner 模式的 `delivered` 表示主模型已成功处理，发布模式的 `published` 仅表示事件入口已接收。关闭管理器时尚未处理的通知变为 `suppressed`。单项输出仍默认限制为 50 KiB。

通知使用带有 `source: "background_subagent"`、`task_id`、`agent`、`task`、`status`、`output` 和 `error` 等字段的 JSON 文本，作为新的 `UserMessage` 交给主模型。原始启动工具调用已经结束，通知不会再次写入对应的工具结果，也不会把子 Agent 的完整历史追加到主历史。每项完成通知只尝试处理一次；主模型错误或主运行被取消时，记录 `notification_status="failed"` 和 `notification_error`，保留任务结果供查询，不自动重试。`failed` 既可能表示通知尚未写入主历史就被取消，也可能表示写入后未处理完；取消当前主运行后，不会立即再次唤醒以重发同一通知。后续通知仍可继续处理，失败记录也遵循下述容量淘汰规则。

应用也可以直接管理任务：

```python
task_id = background.start("reader", "读取 README.md 并概括接口。")
snapshot = background.get(task_id)
all_tasks = background.list_tasks()

background.cancel(task_id)  # 只请求取消，返回是否发出了新请求。
state = await background.wait(task_id)  # 等待子任务及其清理，不等待主 Agent 处理通知。
await background.wait_until_idle()  # 等待全部后台任务和管理器消息队列处理完成。
```

后台任务属于管理器，`runner.cancel()` 只取消当前主对话，不会停止后台子任务；取消指定任务使用 `subagent_cancel` 或 `background.cancel(task_id)`。退出 `async with background`，或显式调用 `await background.aclose()`，会停止接收新任务、取消后台任务并等待清理，同时停止管理器自己发起的主运行。若管理器只是在等待用户直接发起的 `runner.run()`，关闭不会取消该用户运行。已经发生的工具副作用不回滚。

直接 Runner 模式的一个管理器只绑定一个主 Runner，并在同一事件循环中使用。切换会话前应关闭旧管理器。管理器默认最多保留 128 条任务记录；容量用尽时，先淘汰最旧的、通知已结束的终态记录，没有可淘汰记录则拒绝新任务。管理器自身的任务记录与直接投递队列都只存在内存中，不跨进程恢复；发布到下一节事件入口的通知由 SQLite 持久保存。子 Agent 仍共享本机进程和文件系统。

`on_event` 可观察带有 `task_id`、父调用 `call_id`、角色和任务描述的 `BackgroundSubagentEvent`，其 `event` 是子 Runner 的原始事件。事件回调应尽快返回；不能在受管理的模型、工具或回调内等待管理器的 `send()`、`wait_until_idle()`、`aclose()` 等依赖自身结束的操作，管理器会拒绝这类调用。

无需 Key 的完整示例会真实启动后台读取任务、在子任务尚未结束时回复另一条用户消息并查询状态，随后自动通知主 Agent：

```bash
uv run python examples/background_subagents.py
```

## 三层事件运行

`wintermute.events` 在现有 `AgentRunner` 外提供三个 Python SDK 入口。每个入口可以绑定多个终端；同一终端也可以使用多个入口。事件经统一校验、去重后分层保存，再按会话调度模型执行：

```text
交流终端 → conversation ─┐                  ┌→ conversation_events → 用户轮次
通知服务 → notification ─┼→ EventGateway ───┼→ notification_events → 通知轮次
感知设备 → perception ───┘                  └→ perception_events → 当前状态表
                                                    ↓
                         RunScheduler → ContextAssembler → AgentRunner
                                                            ↓
                                                   OutputRouter → 回复终端
```

这一版是应用内接口，不启动 HTTP、WebSocket 或设备连接服务器。上游适配器负责网络连接、用户认证和接入协议，认证后再使用已注册的终端绑定；不能把网络请求自报的 `terminal_id` 当作身份凭据。音视频连续采集、转录和摘要属于上游处理，本模块接收已整理的状态、文本摘要及证据引用。

| 接口或模块 | 职责 |
| --- | --- |
| `gateway.conversation.connect(terminal_id, conversation_id)` | 绑定交流终端；`send(text, ...)` 提交用户消息，回复默认返回该终端 |
| `gateway.notification.connect(terminal_id, conversation_id, ...)` | 绑定通知来源和目标会话；`publish(type, payload, ...)` 提交通知 |
| `gateway.perception.connect(terminal_id, ...)` | 绑定感知来源；`observe(state_key, value, ...)` 更新状态，也可用 `publish()` 保存其他感知事件 |
| `SQLiteStore` | 终端与会话登记、三层事件、去重回执、任务队列、运行记录和输出记录 |
| `RunScheduler` | 同会话串行、不同会话并发；排队的用户消息优先于普通通知 |
| `ContextAssembler` | 按用户与会话范围选择通知和有效感知状态，生成本轮参考资料 |
| `OutputRouter` | 将最终输出投递给指定终端，区分模型处理完成与终端确认 |

下面的片段接入一个已有的 `model`；完整可执行示例见 `examples/event_runtime.py`：

```python
from wintermute.agent import AgentRunner
from wintermute.events import (
    ContextAssembler,
    EventGateway,
    OutputRouter,
    RunScheduler,
    SQLiteStore,
    Terminal,
)

store = SQLiteStore("events.sqlite3")
gateway = EventGateway(store)
gateway.register_terminal(
    Terminal(terminal_id="desktop", user_id="user", allowed_layers=("conversation", "perception"))
)
gateway.register_conversation("work", "user")
chat = gateway.conversation.connect("desktop", "work")
sensor = gateway.perception.connect("desktop", conversation_id="work", source_id="window")
output = OutputRouter(store)


async def receive(record):
    print(record.output_id, record.text)  # 终端按 output_id 去重，成功返回表示确认。


output.connect("desktop", receive)
scheduler = RunScheduler(
    gateway,
    lambda conversation_id: AgentRunner(model),
    assembler=ContextAssembler(store),
    output_router=output,
)

# 在异步函数中执行；应用退出时先关闭 scheduler，再调用 store.close()。
async with scheduler:
    await sensor.observe("active_document", "README.md")
    receipt = await chat.send("当前文档是什么？", producer_event_id="desktop-message-1")
    result = await scheduler.wait(receipt.job_id)
    await scheduler.wait_until_idle()
```

`terminal_id` 标识来源和回复目标，`conversation_id` 标识会话，二者相互独立。多个终端可以连接同一会话，共享已执行的交流历史；不同会话的模型运行与历史分开维护。注册信息约束终端允许写入的层以及用户归属；目标会话和回复终端必须属于同一用户。感知事件可以限定到某个会话，或者作为该用户的共享状态；上下文不会读取其他用户或其他会话的私有资料。

`producer_event_id` 由发送方在重试时复用。接收事务同时保存事件、去重回执，以及必要的运行任务或感知状态更新，提交后才返回 `Receipt`。重复事件返回原 `event_id` 和 `job_id`，不重复创建运行；同一发送 ID 携带冲突内容会报错。需要稳定表示事件发生时间时，应同时保留发送方最初的 `occurred_at`。感知事件默认没有 `job_id`，只更新状态；迟到事件保留在历史中，不覆盖较新的当前值，过期状态不会被选进上下文。

调度器独占该数据库的调度租约，通过心跳续期；已有有效租约时，第二个调度器不能启动。使用文件数据库时，重启后可以继续处理已保存的 `queued` 工作。遗留的 `running` 工作标为 `interrupted`，需要应用明确决定是否重新发起，避免自动重放可能已经执行过的工具。正常运行错误标为 `failed`。事件接收去重和输出去重不能保证外部工具副作用严格只发生一次；需要该保证的操作应使用业务幂等键。

`scheduler.wait(job_id)` 返回终态任务记录，调用方应检查 `status` 和 `error`。用户消息与通知都按会话串行处理，优先级只作用于尚未开始的工作，不中断正在执行的工具。不同会话的并发数由 `max_concurrency` 控制；每个会话使用独立 Runner，模型和工具的共享对象仍须支持并发。

通知处理采用独立的通知轮次，执行结果保存在运行与通知输出记录中，不写回用户交流历史。感知与通知资料以 `references` 传入本轮模型上下文，不进入对话摘要或原始历史。配置 `MainContextManager` 时，这些资料会和系统提示词、工具、对话一起计入预算；摘要器只压缩对话，动态资料不会使历史摘要前缀持续变化。未配置上下文管理器时，不提供模型窗口的总长度保证。资料选择目前按范围和配置预算完成，不包含长期记忆整理或语义检索。

`OutputRouter` 投递最终文本。终端未连接或接收失败时，输出保留为 `pending`；成功确认后标为 `delivered`。调度器定期重试待投递输出，应用也可显式调用 `await output.deliver_pending()`。客户端必须按 `output_id` 去重，避免“终端已收到、服务端未记录确认”时重发造成重复展示。未指定回复终端的通知输出仍保留在存储中，供应用读取。`scheduler.subscribe(listener)` 提供带 `job_id`、`event_id` 和 `conversation_id` 的 `RuntimeEvent`，其中 `event` 是原始 Runner 事件，可用于流式文本与工具进度展示。

后台子 Agent 使用同一通知入口：

```python
from wintermute.events import BackgroundEventPublisher, Terminal
from wintermute.agent.tools import BackgroundSubagentManager, SubagentConfig

gateway.register_terminal(
    Terminal(terminal_id="workers", user_id="user", allowed_layers=("notification",))
)
notifications = gateway.notification.connect("workers", "work", reply_terminal_id="desktop")
background = BackgroundSubagentManager(
    [SubagentConfig("researcher", model)],
    notification_publisher=BackgroundEventPublisher(notifications),
)
# Runner 工厂将 background.tools 注册到相应会话的 Runner；无需 background.bind()。
```

每个会话分别创建管理器和发布桥接器，确保结果进入原始会话。完成、失败和主动取消的结果发布成功后，后台快照的 `notification_status` 为 `published`，表示通知已持久接收；主 Agent 是否处理以及终端是否收到，由事件任务和输出状态表示。发布失败保留 `notification_error`，不自动重试。此模式下 `background.send()` 会拒绝调用，用户消息应通过交流入口提交。

生命周期顺序为：先启动调度器，在其生命周期内使用后台管理器，退出时先关闭后台管理器，再关闭调度器，最后关闭数据库。`scheduler.wait_until_idle()` 不会等待尚未完成的后台子任务；需要等整个流程结束时，先 `await background.wait_until_idle()`，再 `await scheduler.wait_until_idle()`。关闭后台管理器会取消尚未完成的工作及发布，并抑制关闭产生的通知；已进入事件存储的通知不受影响。后台子任务本身仍只在当前进程内运行，不支持断点恢复。

离线示例使用临时 SQLite 数据库，演示两个终端共享会话、感知状态更新、重复发送去重，以及子任务期间继续聊天、完成后经统一入口自动通知：

```bash
uv run python examples/event_runtime.py
```

当前范围不包含音视频持续处理、设备协议适配、跨来源感知融合、长期记忆或多模态模型输入。已有媒体可用引用记录为事件证据，模型适配器仍处理文本和 function tools。

## 模型、工具和事件契约

- **模型**：实现 `stream(context) -> AsyncIterator[ModelEvent]`，先产生零到多个 `TextDelta`，最后恰好一个完整的 `AssistantMessage`。只有完整消息进入历史，工具参数只从最终消息读取。流中断、截断、缺失最终消息、重复工具 ID 等会终止当前运行。
- **工具**：异步执行，按模型声明顺序调用。Pydantic 参数使用 `strict=True` 验证；是否拒绝额外字段由参数模型的 `extra` 配置决定。未知工具、参数错误、执行异常和超时会生成 `is_error=True` 的工具结果，再交给模型处理。
- **取消**：传播到正在等待的模型或工具；已经完成的消息保留，未返回结果的工具调用补上取消结果，保持历史协议完整。取消不能回滚已发生的工具副作用；工具实现必须正确传播取消，不能阻塞事件循环。
- **上下文**：可注入异步 `transform_context(messages)`，随后由可选的 `context_manager` 执行长度检查和自动摘要，不改完整历史。变换后及压缩后的历史都必须保持 tool call/result 配对。
- **边界**：默认每次运行最多 20 个模型轮次，超出抛 `MaxTurnsExceeded`。模型异常向调用方传播，并记录在 `state.error`；可通过 `continue_run()` 重试。尚未消费的队列保留。
- **事件**：`agent_start/end`、`turn_start/end`、`message_start/update/end`、`tool_execution_start/end`、`context_prepared`、`error`。`message_update.delta` 为文本增量；`agent_end.status` 为 completed/cancelled/error。出错或取消时直接发终止事件，未完成的 message/turn 不保证配对。监听器按注册顺序等待，普通异常记录日志并隔离；监听器应尽快返回，不能等待同一 runner 空闲。

核心不依赖特定模型 SDK；OpenAI SDK 是可选依赖。父 Runner 的工具调用仍按顺序执行，`subagent` 内部可并行运行独立子任务。事件、调度和输出持久化由 `wintermute.events` 提供；自动重放失败的模型运行、通用并行工具调度、多模态、供应商 token 用量统计和网络认证仍需上层扩展。

## 项目结构

```text
src/wintermute/
  __main__.py        # python -m wintermute 的配置加载与服务启动入口
  agent/
    __init__.py     # AgentRunner、消息与工具协议的公共入口
    core/
      __init__.py   # 运行核心公共接口
      runner.py     # AgentRunner 状态、循环、队列和取消
      types.py      # 消息、模型协议、事件和快照
      tools.py      # 工具声明及执行上下文
      errors.py     # 基础层异常
    context/
      __init__.py   # 上下文预算、主/子管理器与可替换协议
      types.py      # 预算、状态、计数器与摘要协议
      manager.py    # 主/子 Agent 的增量摘要和安全历史投影
      summarizer.py # 有独立输入预算的模型摘要器
    providers/
      __init__.py   # 模型适配器公共入口
      openai/
        __init__.py # OpenAIModel 默认指向 Responses 适配器
        chat.py     # 异步流式 Chat Completions 适配器
        responses.py # 异步流式 Responses 适配器
    tools/
      __init__.py   # 基础工具与子 Agent 工具的统一入口
      builtin/
        __init__.py # create_builtin_tools 与基础工具导出
        files.py    # read / write / edit
        command.py  # 跨平台 exec
      subagent/
        __init__.py # 前台和后台子 Agent 公共接口
        foreground.py # 子 Agent 配置、独立上下文、并发调度和取消
        background.py # 后台任务管理、通知发布和主 Agent 自动唤醒
  events/
    __init__.py     # 三层事件入口、存储和运行层公共接口
    core/
      __init__.py   # 事件协议和公共类型导出
      types.py      # 事件信封、终端、接收回执、状态和异常
    ingress/
      __init__.py   # 三个入口及终端绑定接口
      gateway.py    # 多终端绑定、统一接收、校验和路由
    storage/
      __init__.py   # 存储实现公共入口
      sqlite.py     # SQLite 分层事件、状态、调度租约和输出记录
    runtime/
      __init__.py   # 上下文组装、调度和投递公共接口
      context.py    # 通知与感知快照、范围筛选和资料预算
      scheduler.py  # 持久队列、会话调度、恢复和流式事件
      output.py     # 终端输出、确认及失败重试
    integrations/
      __init__.py   # 外部模块与事件系统的集成接口
      subagent.py   # 后台子 Agent 结果发布桥接
  config/
    __init__.py     # 配置模型与管理器公共接口
    models.py       # JSON 配置结构、默认值、校验和脱敏展示
    manager.py      # 加载、原子保存、局部更新和显式重载
    factory.py      # 配置到模型、工具、Agent、日志和事件运行组件
  logging/
    __init__.py     # 日志配置、logger 与关联上下文公共接口
    context.py      # 基于 ContextVar 的异步关联字段
    formatters.py   # 文本/JSON 格式与敏感字段脱敏
    setup.py        # 控制台、文件轮转和可关闭的配置句柄
  interfaces/
    __init__.py     # 轻量包入口，不自动加载网络依赖
    http.py         # HTTP 认证、消息提交、任务查询与输出确认
    telegram.py     # Telegram 白名单、长轮询与回复投递
    service.py      # 按配置启动接口、会话调度和后台通知
config/
  settings.example.json   # 可提交的配置模板，不含真实密钥
  interfaces.example.json # HTTP 服务及 Telegram 绑定配置模板
examples/
  mock_agent.py     # 离线工具闭环
  ark_agent.py      # 火山方舟真实接口示例
  builtin_tools.py  # 临时目录中的四种基础工具演示
  ark_tools.py      # 带本地工具的火山方舟 Agent
  subagents.py      # 子 Agent 并行执行与真实文件读取的离线演示
  background_subagents.py  # 后台读取、主对话继续响应和自动通知的离线演示
  context_management.py    # 主/子 Agent 独立预算与自动摘要的离线演示
  event_runtime.py         # 多终端、三层存储、调度与后台通知的离线演示
  configuration.py         # JSON 配置、事件运行和日志的离线演示
  configured_agent.py      # 从 JSON 启动真实模型、主/子 Agent 和事件运行层
tests/             # 默认全部离线，无 API Key 依赖
```

Agent 相关模块统一从 `wintermute.agent` 下导入。运行核心仍可使用 `from wintermute.agent import AgentRunner, Tool`；上下文、模型适配器和工具分别使用 `wintermute.agent.context`、`wintermute.agent.providers` 和 `wintermute.agent.tools`。需要按类别导入时，可使用 `wintermute.agent.tools.builtin`、`wintermute.agent.tools.subagent` 或 `wintermute.agent.providers.openai`。原顶层的 `wintermute.context`、`wintermute.providers` 和 `wintermute.tools` 导入路径已迁移；事件接入与调度继续使用 `wintermute.events`。

事件模块按职责分为 `core`（事件协议）、`ingress`（接入）、`storage`（存储）、`runtime`（组装、调度与投递）和 `integrations`（模块集成）。`from wintermute.events import EventGateway, SQLiteStore, RunScheduler` 等公共导入保持不变，也可按类别从 `wintermute.events.core`、`wintermute.events.ingress`、`wintermute.events.storage`、`wintermute.events.runtime` 和 `wintermute.events.integrations` 导入。此次整理只调整目录和引用，事件接收、存储、调度及通知行为沿用现有实现。

## 开发验证

```bash
uv sync --locked --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

本地工作区保留了离线测试，可运行 `uv run pytest`。按项目约定，`tests/` 在 `.gitignore` 中，因此测试文件不随 Git 提交；新检出的仓库不会自带这组测试。CI 只在测试文件存在时运行 pytest。

CI 配置覆盖 Windows / Ubuntu 和 Python 3.12、3.13、3.14。`uv.lock` 锁定开发与可选依赖；构建的 wheel 包含类型标记 `py.typed`。

## 设计参考

参考 [pi agent-core](https://github.com/earendil-works/pi/tree/89c921d8b56b59e768b26e7f48b6832978119310/packages/agent) 的状态包装器、模型/工具循环、事件和 steering / follow-up 分层，以独立 Python 实现适配 Wintermute。参考版本固定为 `89c921d8b56b59e768b26e7f48b6832978119310`，并非完整 API 移植。

当前实现使用顺序工具、严格输入校验、Python 标准取消语义和显式轮数上限。三层事件运行是 Wintermute 自身的应用层设计；平台接入、长期记忆、人格与更复杂的注意力策略仍将逐步构建。

基础工具参考 pi 的 [read / write / edit / bash / powershell 实现](https://github.com/earendil-works/pi/tree/89c921d8b56b59e768b26e7f48b6832978119310/packages/coding-agent/src/core/tools)：采用分页读取、批量精确编辑、输出截断及命令取消等行为；这里用 `exec` 统一命令工具名称，编辑参数使用 Python 风格的 `old_text` / `new_text`，不移植图片读取、模糊编辑和后台任务功能。

子 Agent 工具参考 pi 的 [subagent 扩展示例](https://github.com/earendil-works/pi/tree/main/packages/coding-agent/examples/extensions/subagent)：采用显式角色配置、独立上下文、单任务与并行委派、事件转发和取消传播；这里使用 `asyncio` 与独立 `AgentRunner` 实例实现，不启动 pi 子进程，也不依赖 tmux。

上下文管理参考 pi 的[自动压缩与安全切分](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/compaction/compaction.ts)和[摘要序列化](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/compaction/utils.ts)：采用预留输出空间、保留近期消息、增量更新摘要及完整工具调用边界。这里为主/子 Agent 提供独立管理入口，并给摘要请求增加自身的输入预算；不移植 pi 的会话文件、usage 校准和超窗恢复重试。
