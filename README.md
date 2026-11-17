# Wintermute

一个逐渐理解你的个人 AI 执事。项目以可复用的 `AgentRunner` 和分层事件运行层为基础，后续继续完善长期记忆、人格和更多终端。

当前已实现 Python 异步 Agent 核心、文件与命令基础工具、前台和后台子 Agent、主/子 Agent 上下文管理、支持多终端和 SQLite 持久化的四层事件运行、当日切片流与独立压缩、可配置的 Telegram 与 HTTP 交流接口、JSON 配置与结构化日志，以及 OpenAI 兼容 Responses / Chat Completions 适配器。默认使用 **Responses API**，保留异步流式输出和工具调用。项目由 [uv](https://docs.astral.sh/uv/) 管理，要求 **Python >= 3.12**，默认开发版本为 3.12。

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

`config/settings.json` 是被 Git 忽略的本地配置；模板不包含凭据。默认使用豆包 Responses 接口，启用 `read` 工具和一个独立的 `reader` 子角色。用户消息和后台通知进入同一份会话历史。后台结果可以在主 Agent 回答途中送达，或在会话空闲时触发回复。示例退出前会等待已启动的任务和通知处理完成。可用 `--conversation other-session` 新建独立会话，用 `--config /绝对路径/settings.json` 指定其他配置。同一个数据库中其他本地会话的待处理事件也会恢复；每个会话独立管理后台任务，结果通知返回原会话。

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
| `events` | SQLite 路径、事件大小、调度并发、租约、投递重试、资料组装预算及 `slices` 切片处理设置 |
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

## 硬件录音上传与音频记忆

`wintermute-esp32` 在录音中短按 BOOT 显示上传确认，默认选择本次录音**从起点开始的前 30 分钟**，触摸可选 15 分钟、1 小时或 2 小时，再短按 BOOT 确认；不足所选时长时上传已录制部分。录音继续进行，已收尾的 Ogg/Opus 分段逐个发送。长按 BOOT 仍用于开始/停止录音。Wi-Fi、服务地址及设备口令在硬件项目的 `config/upload_config.local.h` 配置，详细操作见硬件 README。

服务端校验并保存上传的录音，持久化 `slice.audio` 事件，然后调用 DeepSeek ASR。整段转录保存到 SQLite 后，由独立的总结模型生成摘要。长转录分块后逐级汇总，摘要失败或服务重启时复用已保存的转录。原音频、原始事件、转录与来源事件 ID 均保留。摘要进入 `slice_stream`，与所属会话的感知变化合成一条增量时间线，主 Agent 的上下文预算覆盖整条消息。

新服务可以从专用模板开始；已有 `config/settings.json` 时只合并需要的配置，不必覆盖：

```powershell
Copy-Item config/audio.example.json config/settings.json
$env:ARK_API_KEY = "主 Agent 的模型密钥"
$env:VISION_API_KEY = "截图视觉模型的 API Key"
$env:DEEPSEEK_API_KEY = "DeepSeek API Key"
$env:AUDIO_SUMMARY_API_KEY = "音频总结模型的 API Key"
$env:WINTERMUTE_DEVICE_TOKEN = "与硬件配置一致的设备随机口令"
$env:WINTERMUTE_HTTP_TOKEN = "桌面聊天终端的独立随机口令"
uv run --extra openai --extra interfaces python -m wintermute --config config/settings.json
```

该模板监听 `0.0.0.0:8080`，硬件应使用电脑的局域网 IP；设备和桌面终端均绑定 `local/main`，桌面聊天能参考上传录音的摘要。若已有其他终端，可绑定到相同的用户和会话。普通模板默认关闭音频功能。

| 配置 | 用途 |
| --- | --- |
| `events.slices.audio.enabled` | 启用音频处理，要求 `events.slices.enabled` 同时启用 |
| `events.slices.audio.storage_path` | 音频目录，相对于配置文件目录；默认 `../data/audio` |
| `events.slices.audio.max_segment_bytes` | 每个音频分段上限，默认 4 MiB；独立于 HTTP 文本 `max_body_bytes` |
| `events.slices.audio.asr` | DeepSeek ASR 的 `endpoint`、`api_key_env` 和单次识别请求的 `timeout`（秒） |
| `events.slices.audio.summary_model` | 总结 AI 的独立 `api`、`base_url`、`model`、`api_key_env` 等；启用音频时必须显式配置 |
| `events.slices.max_input_chars / max_output_chars / model_timeout` | 每次摘要的输入、输出预算与处理超时 |

后端使用 Bearer API Key 调用 `POST https://api.deepseek.com/v1/tools/asr`，通过 multipart 的 `file` 字段发送音频，从响应的 `text` 字段读取转录。单次请求的上限为 120 秒、8 MiB。符合限制的设备录音直接以 Ogg/Opus 上传。详见 [DeepSeek 语音识别指南](https://api-docs.deepseek.com/zh-cn/guides/asr/)。

超过任一限制时，后端将录音解码为单声道 16 kHz、16 位 PCM。需要分段时，本地 WebRTC VAD 以 20 毫秒为一帧检测语音，将连续至少 300 毫秒的非语音区间视为停顿。在每段上限前的最后 15 秒内，优先选择最近一次停顿的中点。若上限已经落在停顿里，或窗口内没有合适停顿，就在上限处切分。各段包含完整的声音和停顿，按原顺序封装为 WAV 送去识别，返回的文字合并到原来那条录音事件。

已有配置可将 `events.slices.audio.asr` 整段替换为 `{"provider": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"}`，端点与超时使用默认值。运行上面的启动命令会安装音频分段依赖。ASR、音频总结和主 Agent 分别读取各自配置的凭据。

硬件上传协议：

| 方法与路径 | 行为 |
| --- | --- |
| `POST /v1/audio/slices` | 请求体为一段完整的原始 Ogg/Opus；持久化后返回 HTTP 202、`event_id`、`duplicate`、`status` |
| `GET /v1/audio/slices/{event_id}` | 查询本终端音频的 `pending / retrying / completed`、重试信息及记忆 `item_id` |

两个接口沿用 `X-Terminal-ID` 和 `Authorization: Bearer ...` 认证。上传还须携带 `Content-Type: audio/ogg`、`X-Recording-ID`（跨启动唯一）、`X-Segment-Sequence`、`X-Start-Sample`、`X-Sample-Count`、`X-Sample-Rate: 16000`。每段单声道、至多 300 秒，所选范围不超过录音起点后的 2 小时；服务端验证 Ogg 页顺序、CRC、Opus 头、EOS 与样本数。可选 `X-Recording-Elapsed-Samples` 表示发送时距录音起点的时长乘以 16000，包含传输等待及停止后的时间；硬件据单调时钟提供此值，服务端据此估算分段时间。无该字段的客户端按接收时间减分段时长记时。没有设备实时时钟时，时间仍存在网络延迟误差；跨日分段按起点日期归档。

设备重复上传相同录音 ID、分段序号和内容会收到原回执。同一 ID 更换音频或样本信息返回 409。202 表示接收成功，最终结果可通过状态接口查询。若一次上传拆成多个 ASR 请求，其中任何请求失败，后台都会从该上传段的开头重新识别。完整转录一经保存，重试摘要时直接复用。上传目录与数据库应一起保留或备份，当前不自动清理音频和转录；未完成持久化回执的中断上传可能留下未引用文件。

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
$env:ARK_API_KEY = "主 Agent 的模型密钥"
$env:VISION_API_KEY = "截图视觉模型的 API Key"
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

`terminal_id` 是终端标识，`user_id` 是应用内的用户归属，`conversation_id` 是持久会话。另一个 HTTP 或 Telegram 终端使用相同的 `user_id`、`conversation_id` 即可接续聊天，`terminal_id` 必须不同。不同会话各自保留上下文和后台任务。同一会话共享用户消息、后台通知和主 Agent 的回复，各终端分别领取发给自己的输出。可以增加多个终端绑定，并为 HTTP 终端分别指定凭据环境变量。

HTTP 每个业务请求均携带 `X-Terminal-ID` 和 `Authorization: Bearer ...`。用户、会话和数据来源由服务端根据终端凭据确定。

**从桌面管理模型密钥。** 在 `interfaces.http.terminals` 中给自己的桌面终端加上 `"manage_api_keys": true`，为它分配独立的终端令牌，然后重启服务。桌面端的「设置 → 接口密钥」会列出各项密钥的用途、配置状态和掩码。共用同一环境变量的用途会合并显示。保存的值优先于原配置，后续模型请求立即使用新值。这个权限适用于整个后端的模型服务，默认关闭。

远程桌面使用 HTTPS 地址。如果通过同机反向代理提供 HTTPS，由代理覆盖 `X-Forwarded-Proto` 为实际连接协议。本机调试或 SSH 端口转发可使用回环地址。

桌面修改的密钥保存在后端服务账号的私有配置目录中，按事件数据库的绝对路径区分实例。在同一路径重建数据库后仍可使用原密钥。服务器可用 `WINTERMUTE_SETTINGS_DIR` 指定独立的可写绝对目录，备份密钥时应包含该目录。使用 `:memory:` 数据库的临时服务只在内存中保存修改。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /health` | 无需认证，返回 `{"status":"ok"}` |
| `GET /v1/settings/api-keys` | 返回 `items`，每项包含配置项标识、用途列表、已配置状态和脱敏尾号。仅限具有密钥管理权限的终端。 |
| `POST /v1/settings/api-keys` | 发送 `id` 和新值 `api_key`，成功后返回更新后的脱敏列表。 |
| `POST /v1/messages` | 提交 `{"message_id":"...","text":"..."}`；返回 HTTP 202 和 `event_id`、`job_id`、`duplicate`，无需等待模型完成 |
| `POST /v1/perceptions` | 提交设备感知，如 `{"event_id":"d-1","state_key":"desktop.window_span","value":{"foreground_app":"会议客户端","started_at":"2026-11-14T06:00:00Z","ended_at":"2026-11-14T06:01:00Z","duration_seconds":60}}`。值可以是任意有效 JSON |
| `POST /v1/perceptions/desktop-window` | 接收一次已结束的应用停留及 1 至 4 张代表帧。原图和停留先入库，服务端异步逐图提取文字并补入同一条原始感知记录，按终端和事件 ID 去重。 |
| `POST /v1/perceptions/desktop-window/batches` | 分批接收一次停留的截图；每批最多 4 张，全部到齐后写入一条感知事件。 |
| `POST /v1/slices` | 提交文字切片，如 `{"event_id":"m-1","text":"决定把演示改到周五。"}`。后台切片处理器生成摘要 |
| `GET /v1/jobs/{job_id}` | 查询授权范围内的任务；返回 `status`、`error` 和 `outputs` 等字段 |
| `GET /v1/schedule?start=2026-11-01T00:00:00Z&end=2026-12-01T00:00:00Z` | 桌面日历按当前会话读取安排。起点包含、终点排除，两端均需时区。按触发时间分页，支持 `limit`（1–100）和 `cursor` |
| `GET /v1/profile` | 读取认证终端所属用户的当前档案，无请求参数。返回 `{status, markdown, effective}`。`empty` 返回初始模板，`ready` 返回已有资料，`unconfigured` 表示该用户尚未配置档案路径。`effective` 表示按当前内容组装下一轮主 Agent 系统提示时是否会加入档案。内部标记和已过期的自动条目不会显示。文件暂时不可读时返回 503。 |
| `GET /v1/notifications` | 当前终端的用户提醒，按新到旧分页（`limit`、`cursor`）。`pending=1` 只取待接收，`unread=1` 只取未过期且未读的提醒。每条含附件名称、字节数、类型和 ID |
| `POST /v1/notifications/{id}/received` 或 `/read` | 确认当前设备已收到或用户已读，返回更新后的提醒；重复请求可安全重试 |
| `GET /v1/notifications/{id}/files/{file_id}` | 用当前终端凭据下载所选附件，`HEAD` 可读取文件头信息。只有收到这条提醒的绑定终端有权访问 |
| `GET /v1/outputs?limit=100` | 返回 `{"outputs":[...]}`，包含当前终端、当前会话尚未确认的普通回复及后台结果通知 |
| `POST /v1/outputs/{output_id}/ack` | 确认终端已收到指定输出；重复确认安全，返回 `status: "delivered"` |
| `GET /v1/history?view=chat&limit=40` | 分页读取历史，`view` 可选 `conversation`（交流）、`notification`（通知）、`perception`（感知）、`slice`（切片）、`chat`（完整会话）或 `timeline`（会话中的活动时间线更新） |
| `GET /v1/history?view=dialogue&limit=40` | 聊天页读取当前会话的用户原话和主 Agent 最终回复，包括时间表等后台通知触发的回复。按入库顺序从新到旧分页 |
| `GET /v1/history?view=slice&grouped=1` | 在切片历史中按活动折叠已处理的定时截图。每组返回应用、当前对象、首末采样时间、截图数、变化次数和最近事实 |
| `GET /v1/history/screenshot-groups/{id}` | 分页读取本组的截图，按采集时间从新到旧排列。每帧提供新增事实和可下载的原图文件名，沿用 `limit` 和 `cursor` |
| `GET /v1/history/compression?layer=perception` | 列出包含感知记录的活动时间线压缩批次。用 `layer=slice` 查看包含切片的批次 |
| `GET /v1/history/compression/{id}` | 查看批次内可见的感知和切片、已保存的规则摘要、AI 摘要及所属的跨批次汇总 |
| `GET /v1/history/compression/sources/{id}` | 分页读取历史条目对应的原始上报、录音转写或截图文件名，以及切片提取结果。从批次成员展开时附加 `activity=1` |
| `GET /v1/history/files/{id}` | 使用历史列表返回的文件 ID 下载该会话可见的截图或录音，沿用终端认证 |

「记忆 → 感知层」默认显示「格式化信息」，可查看来源设备、应用停留、工作对象变化和画面识别结果。「原始信息」展示完整的已入库事件 JSON，对应 `GET /v1/history?view=perception` 每行的 `raw`。「压缩信息」按批次展示规则摘要和已生成的 AI 摘要，可展开原始资料核对。切片层的批次从「压缩对照」打开。各批次只返回本会话可见的来源。

客户端的聊天页使用 `dialogue` 作为历史来源。每条带有稳定的 `id`，以及值为 `conversation` 或 `notification` 的 `layer`；用户发言还带有发送端的 `terminal_id` 和提交时的 `message_id`。客户端按后端顺序显示记录，用首次同步的结果替换本地历史，并在刷新时核对最新记录。空列表表示当前会话还没有消息。新版保存回复完成时刻，升级前已存在的回复时间为 `null`。

历史的读取范围由终端绑定的用户和会话决定。同一会话的桌面端可以翻阅手机或其他终端留下的记录，以及该用户共享的感知和切片。历史查询返回 `items` 和 `next_cursor`。继续向前翻页时原样传回上一页的 `next_cursor` 作为 `cursor`，值为 `null` 表示已经到达开头。每条记录有角色、来源和正文，后端记录了时间的条目也会带上时间。活动时间线以及完整会话里的时间线消息只展示文字，`files` 为空。感知或切片的历史记录可通过 `files` 引用原图。桌面端在记忆页自动加载可见的截图，录音等其他附件仍按文件名下载。浏览历史不会确认收件箱投递。

请求重试时复用原 `message_id`，相同消息不会重复执行；同一 ID 改成其他内容会返回 409。新消息使用新的 ID。每条消息有自己的 `job_id`。同一终端在回答途中补充的内容可以与原消息共同得到一份回复，此时查询这些任务会返回同一个 `output_id`。缺少或错误的认证返回 401，其他终端无权读取的任务或输出返回 404。GET 查询不会确认输出，客户端展示或保存完成后再调用 `ack`；客户端也可按 `output_id` 去重。

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

### 桌面、手机和 ESP32 接入同一会话

桌面端开启“采集桌面活动”后在本机采样，同一应用停留满 5 秒，在离开后上报一条 `desktop.window_span` 感知事件。事件包含前台应用、窗口标题、起止时间和采样估算的停留秒数。同一应用内稳定的文档、窗口或标签标题依次记入 `value.work_objects`。启用代表帧采集时，这段停留的视觉识别文字记入同一事件的 `value.visual_observations`。

**截图视觉模型。** 桌面感知使用 `events.slices.image.model` 指定的独立视觉模型及 `api_key_env` 密钥（示例为 `VISION_API_KEY`）。开启 `events.slices.image.enabled` 即可识别桌面代表帧。移动端的图片切片还需开启 `events.slices.enabled`。

将下面的设置放在 `events.slices.image.model` 下即可使用方舟 Responses 接口。

```json
{
  "api": "responses",
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "model": "doubao-seed-2-0-lite-260428",
  "api_key_env": "VISION_API_KEY",
  "timeout": 60
}
```

截图处理器使用这里配置的服务地址、协议、模型和凭据创建独立客户端。可指定支持图像输入的 Responses 或 Chat Completions 模型，`api` 分别填 `responses` 或 `chat_completions`。`timeout` 是一次截图描述的超时秒数，`events.slices.max_output_chars` 控制描述长度。

桌面自动代表帧与停留一同发给 `POST /v1/perceptions/desktop-window`。请求沿用感知的 `event_id`、`occurred_at`、`state_key` 和 `value`，另带按采集时间排列的 `frames`。每帧提供 `occurred_at`、可选 `window_title` 及 `jpeg_base64`，每次最多四张，每张 JPEG 不超过 4 MiB。服务端先保存原图，并在同一数据库事务内记录感知事件和每张图的待识别任务，随即返回 202。事件的 `payload.frames` 保存拍摄时间、可选窗口标题和图片校验值。后台直接调用独立视觉模型，每张图的完整返回正文按拍摄顺序补入同一原始事件的 `payload.value.visual_observations`。其中 `frame_index` 与原图一一对应。顶层 `payload.image_recognition` 为 `pending`、`awaiting_model`、`retrying` 或 `ready`。单帧失败由后端退避重试，重启续做未完成任务。桌面停留先进入活动时间线，全部图片识别完毕后再一起补充画面文字，补充记录保留原采集时间和来源，不重复累计停留时长。无图停留通过 `/v1/perceptions` 提交。

桌面端按连续的窗口标题采样，满 10 秒截首张，此后每分钟截一张，每段只上传首、中、末最多三张。一次停留超过四张时使用 `/v1/perceptions/desktop-window/batches`，每批属于同一标题时段。请求另带帧偏移 `offset`、总帧数 `total_frames` 和整次停留的 `manifest`。校验串按帧顺序拼接 UTF-8 时间、NUL、标题（无标题为空）、NUL、小写 JPEG SHA-256 和换行，再取 SHA-256。中间回执返回 `next_offset`，最后一批完成整次采集的原始入库后返回 `event_id`。离线重试沿用同一事件 ID 和清单；后端重启或暂存空闲 30 分钟后，客户端可从首批重传。

移动定时截图可在切片历史中按组浏览，打开组内明细时自动预览可见的截图。识别聊天截图时用发言者和可见原文核对最近事实，没有新增事实的帧仅更新组内的采样时间和数量。有新事实的帧按采集时间进入活动时间线。纯截图切片批次按新增事实的 UTF-8 字节数触发后台摘要。

**移动端定时截图。** 移动端通过 `POST /v1/image/slices` 上传至多 4 MiB 的 JPEG，携带 `X-Event-ID` 和 `X-Occurred-At`，请求带有 `X-Capture-Context`，值为 UTF-8 JSON 的 base64url 编码。字段包括 `platform`、应用、可选标题、包名、使用起点和使用记录 ID。后端保留采样时间和关联 ID，按终端识别重复提交。

在本地服务配置中将 `interfaces.http.terminals` 设为下列数组。为三个环境变量分别设置不同的随机口令，并在对应设备填写同一口令。将 `interfaces.http.host` 设为设备可以访问的局域网地址，手机和 ESP32 使用服务电脑的地址和端口。跨网络部署应通过 HTTPS 保护终端凭据。

```json
[
  {"terminal_id":"http-desktop","user_id":"local","conversation_id":"main","token_env":"WINTERMUTE_HTTP_TOKEN"},
  {"terminal_id":"http-mobile","user_id":"local","conversation_id":"main","token_env":"WINTERMUTE_MOBILE_TOKEN"},
  {"terminal_id":"esp32-recorder","user_id":"local","conversation_id":"main","token_env":"WINTERMUTE_DEVICE_TOKEN"}
]
```

桌面 HTTP 终端只提交对话与感知。自定义桌面终端 ID 时，在该终端配置中设置 `"device_type": "desktop"`；默认的 `http-desktop` 和 `desktop` 自动识别为桌面。三台设备都可以提问和领取本机的回复。桌面端用于观察工作状态。手机端可按独立开关分享应用内活动、地点变化（`mobile.location`），Android 还可在授予使用情况访问权后把其他 App 的前台使用时段上报为文字切片。地点和使用时段通过已有接口进入会话活动时间线。ESP32 用于在桌边提问、按键录音和分享录音器状态。接入 Ogg/Opus 录音时，使用上面的音频配置并开启独立 ASR 和音频摘要；普通对话、状态和文字切片可直接使用 HTTP 模板。

两种采集接口均支持可选的 `occurred_at`（含时区的 ISO 8601 时间），省略时使用服务端接收时间。设备应在采集时生成稳定的 `event_id` 并在重试时复用。提交成功返回 HTTP 202 和 `{"event_id":"服务端事件 ID","duplicate":false}`，同一终端重发同一数据返回原事件 ID 和 `duplicate:true`，复用 ID 更改内容返回 409。文字切片进入时间线需要启用 `events.slices.enabled`。

例如，桌面报告已打开会议客户端，手机记录“决定把演示改到周五”，随后用户在 ESP32 问“会议定了什么？”。前两次采集各自落库，均不发起主 Agent 回复；切片处理完成后，两条变化在下次模型请求中合成一条带来源和时间的活动更新，ESP32 的提问是下一条用户原话。回复投递给 ESP32，用户随后在手机追问时，Agent 仍能看到此前的提问和回答。可运行 `uv run --extra interfaces pytest -q tests/test_terminal_capture_http.py` 在本机复现整个链路。

桌面端“记忆”可以分页翻阅此前的对话和时间线。后续在同一局域网完成三台真机的会议联测，包括断网重连和重启取回待收回复，再根据实测延迟分别接入桌面流式回复、手机后台到达提醒，并测量 ESP32 查询频率与续航。

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

接口格式参考 [OpenAI Responses 文档](https://developers.openai.com/api/docs/guides/migrate-to-responses)及[流式事件文档](https://developers.openai.com/api/reference/resources/responses/streaming-events)。两个适配器均支持文本、function tools 和截图切片的独立看图请求。第三方服务需要支持所选接口及模型。

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

`references` 接受 `UserMessage` 序列，与普通对话历史分开保留。Runner 在压缩完成后将其标为外部资料并展开到模型输入；它们只对本次 `run()` 有效，不进入历史摘要。`record_history` 默认是 `True`，可独立于 `references` 使用；临时运行设为 `False` 时，也不会消费原有的 steering / follow-up 队列。

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

主 Runner 的管理器按会话创建。使用 SQLite 事件调度器和内置上下文管理器时，服务保存已采用的摘要、覆盖的原始消息数及前缀指纹。重启后，前缀校验通过就从同一位置恢复“摘要＋后续原文”。SQLite 中的事件、原话和工具调用记录可继续查询。独立使用 SDK 的 Runner 默认在内存中保存摘要。子角色的 `context_manager_factory` 在每个实际子任务启动时创建独立管理器。

调度器同时记录主模型已有效响应的请求包含了哪些历史。摘要进行中到达的用户更正和后台通知仍作为未读原文进入后续主模型请求，同时接纳的活动保留合并后的时间线。主模型已经读取的旧内容可以按预算压缩；如果未读原文本身超过预算，管理器会报告 `ContextLimitError`。

自定义上下文管理器需要保留 `AgentContext.reference_messages` 和 `reply_task`，并按 `context.for_model()` 展开后的实际输入计数。受控会话里的 `transform_context` 和自定义管理器还需原样保留从首条未读输入开始的消息尾部。Runner 在调用模型前检查这一点。直接实现 `Model.stream()` 时读取 `context.messages` 即可。

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
| `subagent_start` | `{agent, task}` 或 `{tasks: [{agent, task}, ...]}` | 最多 8 项一批，整批预检后立即返回 `tasks` 数组，各项含供后续查询和取消的 `task_id` 句柄、`agent` 和 `status` |
| `subagent_status` | `{task_id}` 或 `{}` | 查询单个任务的 `task` 状态快照，或列出 `tasks` 中的全部保留记录 |
| `subagent_cancel` | `{task_id}` | 请求取消，返回 `cancel_requested` 和当前 `task` 状态；清理结束后状态变为 `cancelled` |

任务状态依次为 `queued`、`running`，终态为 `completed`、`error` 或 `cancelled`；请求取消但尚未完成清理时为 `cancelling`。状态快照还包含任务描述、最终输出、错误、模型轮数、输出截断标志，以及独立的通知状态。通知状态包括 `none`、`pending`、`processing`、`published`、`delivered`、`failed` 和 `suppressed`；直接 Runner 模式的 `delivered` 表示主模型已成功处理，发布模式的 `published` 仅表示事件入口已接收。关闭管理器时尚未处理的通知变为 `suppressed`。单项输出仍默认限制为 50 KiB。

直接绑定主 Runner 时，`background.send()` 发送的用户消息标为 `用户原话 · SDK`，来自子角色的通知标为 `后台通知 · reader` 等来源。主模型先看到原正文，再看到分隔线下的来源和时间。完成时正文为子任务输出，失败和取消时正文说明相应结果。后台通知在主历史中是独立的 `user` 消息。每项完成通知只尝试处理一次；主模型错误或主运行被取消时，记录 `notification_status="failed"` 和 `notification_error`，保留任务结果供查询，不自动重试。`failed` 既可能表示通知尚未写入主历史就被取消，也可能表示写入后未处理完；取消当前主运行后，不会立即再次唤醒以重发同一通知。后续通知仍可继续处理，失败记录也遵循下述容量淘汰规则。

模型从 `subagent_start` 获得的 `task_id` 是操作句柄，传给 `subagent_status`、`subagent_cancel` 即可管理同一任务。SQLite 后台通知的 `correlation_id` 用于内部追踪。应用可用 `background.get(handle).task_id` 将操作句柄对应到该内部标识，也可以直接管理任务。

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

## 四层事件运行

`wintermute.events` 提供交流、通知、感知、切片四个入口。每个入口可以绑定多个终端，同一终端也可使用多个入口。主 Agent 从所属会话的历史读取四层输入。

```text
交流终端 → 用户原话 ─────────┐
后台服务 → 有来源的通知 ─────┴→ 创建回复任务 ──┐
                                             ├→ 会话历史 → 主 Agent → 回复终端
感知设备 → 状态变化 ─────────┐                 │
切片设备 → 压缩完成的摘要 ───┴→ 活动更新 ──────┘
```

事件 SDK 本身不监听网络；HTTP 与 Telegram 适配器见前节。其他上游适配器负责网络连接、用户认证和接入协议，认证后再使用已注册的终端绑定；不能把网络请求自报的 `terminal_id` 当作身份凭据。连续录音由硬件采集；软件切片层既接收文本，也接收已经保存的音频引用，并在后台完成转录与摘要。

| 接口或模块 | 职责 |
| --- | --- |
| `gateway.conversation.connect(terminal_id, conversation_id)` | 绑定交流终端；`send(text, ...)` 提交用户消息，回复默认返回该终端 |
| `gateway.notification.connect(terminal_id, conversation_id, ...)` | 绑定通知来源和目标会话；`publish(type, payload, ...)` 提交通知 |
| `gateway.perception.connect(terminal_id, ...)` | 绑定感知来源；`observe(state_key, value, ...)` 更新状态，也可用 `publish()` 保存其他感知事件 |
| `gateway.slice.connect(terminal_id, ...)` | 绑定切片来源；`record(text, ...)` 提交文本，`record_audio(audio, ...)` 提交已持久化音频引用 |
| `SQLiteStore` | 终端与会话登记、四层原始事件、切片流与处理状态、去重回执、任务队列、运行记录和输出记录 |
| `RunScheduler` | 每个会话最多一个活动 Runner，接纳运行中的用户消息和后台通知，允许不同会话并发 |
| `SliceProcessor` | 独立批量处理切片，按发生日期和来源压缩，原子提交切片流 |
| `ContextAssembler` | 读取持久会话历史，为发起回复的交流或通知组装输入 |
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

`terminal_id` 标识发送终端，`reply_terminal_id` 指定回复终端，`conversation_id` 标识会话。多个终端可以连接同一会话，共享已经接纳的历史。不同会话的模型运行与历史分开维护。注册信息约束终端允许写入的层以及用户归属。目标会话和回复终端必须属于同一用户。感知事件可以限定到某个会话，也可以作为该用户的共享状态。

`producer_event_id` 由发送方在重试时复用。接收事务同时保存事件、去重回执，以及必要的运行任务或感知状态更新，提交后才返回 `Receipt`。重复事件返回原 `event_id` 和 `job_id`，不重复创建运行；同一发送 ID 携带冲突内容会报错。需要稳定表示事件发生时间时，应同时保留发送方最初的 `occurred_at`。感知和切片的原始事件也有接收回执，`job_id` 为空。由它们产生的活动更新没有单独的任务回执或回复。

**用户档案。** 在 `events.profile_files` 中把用户 ID 映射到该用户的 UTF-8 Markdown 档案，例如 `{"local":"../data/profiles/local/PROFILE.md"}`。相对路径以配置文件所在目录为准。可从 [PROFILE.example.md](config/PROFILE.example.md) 复制一份填写，内容应适用于该用户共享的会话。每次交流或通知触发主 Agent 时，后端读取档案，将填写的正文拼到原系统提示词末尾。同一轮的工具续跑沿用本轮版本，用户当前明确表达的要求优先。

档案独立于对话历史。桌面端「记忆 → 用户档案」从首次使用起就展示初始模板和生效状态。查看模板不会创建档案文件。空栏目仅供展示，常用时间段供后台判断空闲时段。主 Agent 在下一轮交流或通知时读取已填写、仍有效的资料。文件上限为 64 KiB，渲染后的系统提示词档案段上限为 8 KiB，超限时本轮跳过档案并记录警告。若要彻底清空用户数据，应同时删除该用户的档案文件。

**空闲时自动整理。** 服务会为 `events.profile_files` 列出的用户启动后台整理。第一次有可保存的信息时，服务会创建目录和档案文件。每位用户需要独立的文件，服务账号需要有目录写权限。用 `events.profile_dreaming.enabled=false` 可暂停整理并继续读取已维护的档案。默认使用主模型配置发起单独的无工具请求；也可以在 `events.profile_dreaming.summary_model` 指定模型、接口和 `api_key_env`。完整配置见 [接口服务示例](config/interfaces.example.json)。

默认每分钟检查一次。规则按 `events.slices.timezone` 统计最近 28 个完整本地日的桌面和安卓前台时段，工作日和周末分别计算，同一半小时内多设备的重叠使用只计一次。某类日期至少有三天样本，且在至少半数样本日出现过的半小时才列为常用时段。进入低活跃时段、距上一条用户交流满 30 分钟且该用户没有排队或运行中的主 Agent 任务时，后台整理新增交流。样本不足或未形成规律时等待 60 分钟。最早未处理的交流积压满 24 小时后，在主 Agent 空闲时处理。只有设备活动时，规则可以更新统计，模型无需运行。

每批按接收顺序读取用户原话，同会话紧邻的助手回复只帮助理解指代。模型提出新增、纠正或删除条目，每个改动须引用本批原话。近期状态到期后退出系统提示词，未说明期限的近期状态默认七天后复核。AI 整理期间可继续聊天；提交前会检查是否出现新交流，并保留用户手工编辑的栏目。SQLite 保存处理位置与待发布版本，Markdown 通过同目录原子替换发布，重启会接续未完成的批次。

**模型看到的内容。** 交流、活动时间线和后台通知在协议中都使用 `role="user"`。用户原话和后台结果各占一条，正文后的分隔线标出来源和时间。模型收到的用户消息例如

```text
只告诉我漏了哪一项，先别改清单。

---
来源：用户原话 · telegram
时间：2026-11-13T06:38:59Z
```

每次主模型请求前，后端将本会话尚未收到的感知变化和切片文字合并为一条时间线 `user` 消息。桌面前台停留明细直接写起止时间、采集到的停留秒数、应用和可用的窗口标题，行尾用 `| 来源设备：http-desktop` 这样的标记注明实际采集终端。其他活动逐项标明时间和来源。迟到或修订的活动带上说明和原发生时间。没有新的内容时沿用历史。下面两项在读取清单期间到达，下一次请求中它们共享同一条消息。

```text
2026-11-13T06:39:00Z | 感知 · desktop | network：正在测试断网重试
2026-11-13T06:39:01Z | 切片 · esp32 | 清单核对期间，会议又确认录音上传已列入清单。

---
来源：活动时间线
时间：2026-11-13T06:39:01Z
```

后台通知沿用相同后缀，正文是子角色返回的结果，来源形如 `后台通知 · worker · reader`。

**主 Agent 时间表。** 可以在对话中说「明天上午九点，把周五交付清单拿出来核对」。主 Agent 使用 `schedule` 工具创建一次性安排，保存标题、正文、触发时间和动作 `notify_agent`。工具的 `list` 返回当前时间和配置时区，便于解释相对日期。待触发的安排可以改期、改内容或删除，已经触发的安排也可查询及删除。删除日历条目后，已送达的通知仍可在通知历史中查看。

桌面端侧栏的「日历」按本机时区展示当前会话的安排。调度器到点后将安排写入通知层，主 Agent 收到来源为「后台通知 · 时间表」的输入。若届时服务离线，恢复后会补发到期通知。每项安排的触发状态和通知在同一笔事务中保存。这一版的通知目标是主 Agent 自身。

**调用终端工具。** 主 Agent 用 `terminal_tool` 的 `list_targets` 查看本会话的终端 ID 和最近在线提供的工具。Windows 桌面端可启用 PowerShell 和 CMD，macOS 提供 sh。安卓和 ESP32 提供只读的 `device_status`。每次 `run` 指定一台设备，如下例。

```json
{"operation":"run","terminal_id":"http-desktop","tool":"powershell","arguments":{"command":"Get-Location"},"timeout_seconds":10}
```

查询手机状态时，使用手机的终端 ID、`"tool":"device_status"` 和 `"arguments":{}`。命令行以桌面登录用户权限运行。设备通过 HTTPS（本机调试可用回环 HTTP）和各自独立的终端凭据向 `POST /v1/terminal-tools/claim` 声明工具并领取调用。工具只出现在已启用且最近轮询的设备上。

工具先返回调用 ID。终端执行后向 `POST /v1/terminal-tools/{id}/result` 提交领取令牌、状态、输出和退出码。后端在同一笔事务中保存结果并通过「后台通知 · 终端工具」唤醒原会话，主 Agent 也可用 `get` 按 ID 查询。排队 15 秒未领取的调用会过期。已经领取的调用不再派发，如果执行时限过后 30 秒仍未收到结果，会标记为结果未知。回传可重试，每次调用最多生成一条完成通知。标准输出和标准错误各保留最多 8 KiB，桌面「记忆 → 终端工具调用」可查看各设备的调用记录，数据来自 `GET /v1/terminal-tools/history`。

**给用户发送文件。** 主 Agent 先用 `notify_user` 的 `list_targets` 查当前会话的设备，再用 `send` 发给一台或多台。每台 HTTP 终端可在配置中设置 `display_name` 以便识别。下面的调用把工作目录下的文件发送给两台设备。

```json
{
  "operation": "send",
  "targets": ["http-desktop", "http-mobile"],
  "title": "会前材料",
  "body": "请带上这份报价单。",
  "attachments": ["会议/报价单.pdf", "会议/议程.txt"]
}
```

附件可填写 `tools.cwd` 下的相对路径，也可用 `{"history_file_id":"evt_…"}` 引用当前用户和会话可访问的历史截图或录音。每条最多 4 个文件，单个最多 8 MiB，合计最多 16 MiB。服务端在发送时保存文件快照，设备只在用户点选后下载。正文和附件可任选其一，标题必填。`expires_at` 可设置带时区的过期时间。用户提醒和每台设备的回执保存在事件数据库，已入队、设备收到、用户已读分别记录。主 Agent 可以在收到时间表的 `notify_agent` 事件后调用该工具。

**持续压缩活动。** 采集事务保存原始观测，并按用户、会话可见范围和设备维护活动批次。规则按时段合并应用停留，累计实际报告的秒数和次数，保留窗口标题。两次观测之间的空档单独处理。切片参与汇总的是文字描述。后台压缩即使整天没有对话也会运行。

`events.timeline_compression` 的默认参数如下。长度沿用项目的保守估算，每个 UTF-8 字节按一个预算单位计。供应商实际计费的 token 数会有差异。

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `raw_tokens` | 1500 | 原始观测累计达到此值即封存一批，后续输入另起一批 |
| `idle_seconds` | 900 | 空闲达到此时长也封批；跨本地日期另起一批 |
| `ai_threshold` | 600 | 封存批次的规则文本仍超过此值时，后台异步生成 AI 摘要 |
| `summary_tokens` | 300 | 单次压缩结果的长度上限 |
| `delivery_tokens` | 800 | 一次投递给主模型的活动消息总预算，包含时间和来源后缀 |

同一设备、可见范围和本地日期内，每六个已封存的小批次可生成一份时段摘要。对于桌面停留，规则会跨批次累计时长并合并重复窗口标题。只有六批都完整落在本次投递范围内且顺序连续时，才复用时段摘要。待发活动超出投递预算时，时间线为较早活动保留概览，优先展示最近细节。主 Agent 可调用 `search_activity` 按时间、设备或关键词分页回查该会话可见的活动明细。

AI 压缩独立于主对话。新活动照常入库，用户此时发言可以立即使用已就绪的摘要和最新规则文本。批次的摘要写入与各个会话的投递进度分开保存；摘要完成只更新该批次的可读表示。模型超时或失败后会退避重试，重启后从数据库继续。设置 `ai_enabled: false` 可只使用规则。`summary_model` 可以指定独立的模型端点；默认优先使用已启用的音频摘要模型，其次使用主模型端点发起独立请求。

三类消息按顺序保存在会话历史中，均可参加对话摘要。`store.history()` 返回的 `UserMessage` 在 `text` 保留正文，在 `source`、`timestamp`、`origin` 保留类别、时间与来源。可以调用 `message.model_text()` 查看发送给模型的文本。原始事件 ID、关联 ID 和活动的来源事件列表可在 SQLite 的事件和活动记录中追溯。事件调度器的 `references` 为空。

**活动进入哪个会话。** 设备指定 `conversation_id` 时，活动只进入该用户的指定会话。用户级活动进入当时已经注册的所属会话。新注册的会话从之后收到的第一条活动开始记录，即使用户还没有发言；注册前的资料不会自动补进来。感知按会话范围、设备、来源、事件类型和状态对象比较内容。重复报告同一个文档不会产生新消息，从 A 切到 B 再回到 A 会留下三次观察。切片摘要以原始片段为依据，音频用录音 ID 和分段序号识别，同一批文本用来源事件 ID 识别。不同设备的观测和不同录音段的相同摘要各自保留。活动保留观察发生时间 `occurred_at` 与系统获知时间 `known_at`，迟到的切片摘要参与下一次时间线比较，已发出的消息保留原文。已过期的感知记录保留有效期，超过当前时间的来源时间会附上待确认说明。

**新输入如何插入。** 空闲时，交流和通知发起回复。感知变化和已完成的切片先进入活动存储。Runner 在准备下一次模型请求时一起读取活动。如果在生成最终回复期间又有活动变化，Runner 在提交回复前最多补读一次。这样即使设备持续上报，用户也能收到答复。补读期间到达的活动留给下次模型请求。不同会话由 `max_concurrency` 控制并发。

- **生成回答时**，用户引导或后台通知到达，调度器会让 Runner 结束当前模型请求，等待流关闭，再带着新增历史请求模型。启用了流式订阅时，已经播出的非空文本作为带中断标记的助手片段保留，订阅方会收到带 `turn` 的 `turn_interrupted`。尚未输出文字时，历史中没有空助手消息。未形成完整声明的工具调用会被丢弃。
- **执行工具时**，已经启动的工具先完成，真实结果写回对应的 `tool_call_id`。用户引导或后台通知到达时，同批尚未启动的调用逐个记录跳过结果。只有设备活动到达时，工具批次照常执行。工具结果齐全后，Runner 合并待发时间线并接纳新输入。例如读清单时用户说“先别改”，正在执行的读取会返回，排在它后面的写入会跳过。

**回执与投递。** `scheduler.wait(job_id)` 返回终态任务记录，调用方可检查 `status` 和 `error`。同一答复中，回复目标相同的输入保留各自的任务，但关联到同一份最终输出。同一会话里来自另一回复终端的输入也可以立即进入共享历史，调度器随后为该终端单独生成答复，沿用已入史的原话。最终输出提交之后到达的交流或通知会形成下一次答复。

同一运行遇到多个回复目标时，模型输入末尾会附上当前终端的待答任务，列出该终端的待答输入，每条都带时间、来源和正文。即使对话前缀已被压缩，模型仍能看到要回应的内容。该任务参与本次请求的预算，持久历史中只保留原始输入。

**持久化与恢复。** 调度器独占数据库调度租约并通过心跳续期。重启后继续处理 `queued` 工作，将遗留的 `running` 工作标为 `interrupted`。已完成的工具保留结果，故障前已启动但未记录结果的调用会标明执行效果未知。普通运行错误标为 `failed`。需要重试的外部操作由应用核对执行情况并使用业务幂等键。

SQLite 事件库使用模式版本 16。版本 3 至 15 的库可就地升级，保留已有会话、原始事件与附件。版本 16 新增桌面感知截图的待提交标记，供服务恢复中断的附件写入。数据库还保存主模型读取位置、会话摘要检查点、活动压缩批次、定时截图分组、旧桌面快照的去重回执、时间表、聊天历史索引、用户通知与附件快照，以及档案整理进度。升级会用已保存的摘要为历史定时截图建立浏览索引，只在采集范围和时段一致且原图相同时折叠旧帧。这个过程在本地完成，没有视觉模型费用。更旧的数据库请保留原文件，将 `events.database_path` 指向新的数据库文件。旧配置中的 `max_notifications`、`max_states`、`max_slice_items` 和 `max_reference_bytes` 已移除。活动消息使用独立的投递预算，整段对话的模型输入预算由主 Agent 的上下文管理器计算。

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

会议示例使用离线模型、临时 SQLite 数据库和真实的文件工具。用户先让主 Agent 核对会议切片与当前演示清单，随后补充“只告诉我漏了哪一项，先别改清单”，后台 `reader` 则发布核对结果。示例分别在生成首字前、输出文本中和读取清单时插入输入，检查落盘的历史、清单内容及最终回复。

```bash
uv run python examples/meeting_interruptions.py
```

加 `--timeline` 可单独运行工具执行中的会议场景，展开主模型实际收到的消息。首次请求包含会前五条活动合成的一条时间线，以及用户问题。第三次请求包含工具执行期间新增的两条活动合成的一条增量，以及用户引导和后台通知。再传 `--dump-requests meeting-requests.json` 可将各次完整请求保存为 UTF-8 JSON。

用 `--compaction` 验证同一会议的长期会话。示例读一次实际清单，用小窗口触发旧会议记录压缩，在摘要调用期间插入“只告诉我漏了哪一项，先别改清单”和后台核对结果。随后关闭并重新打开 SQLite，启动全新的 Runner，检查下一次请求能否直接接续已存摘要。主模型、摘要模型均离线运行。

```bash
uv run python examples/meeting_interruptions.py --compaction --dump-requests logs/meeting-compaction.json
```

原始录音和截图通过已认证的 HTTP 入口接收，以文件引用记录来源。后台切片处理器负责音频转录和截图描述，主会话在模型请求前读取时间线文本。

### 切片入口与当日切片流

文本切片表示一段记录，例如一次设备操作、一段已转录的谈话或一条活动描述。音频切片保存已上传音频的引用，处理流程见前面的“硬件录音上传与音频记忆”。原始事件保存在 `slice_events`，不覆盖感知状态，也不创建主 Agent 的运行任务。独立的 `SliceProcessor` 在调度器后台工作，将同一用户、会话范围、终端、来源、自然日的文本分批处理；每批文本或每段音频产生一个持久化流条目，多个条目按发生时间组成当日切片流。原始正文和 `metadata` 留在事件表中，音频转录保存在 `audio_transcripts`，流条目保留原始事件 ID，便于追溯。

```python
from datetime import UTC, datetime
from wintermute.events import Terminal

gateway.register_terminal(
    Terminal(terminal_id="glasses", user_id="user", allowed_layers=("slice",))
)
# 省略 conversation_id 表示同用户共享；填写时只供该会话使用。
slices = gateway.slice.connect("glasses", source_id="transcript", conversation_id="work")
occurred_at = datetime.now(UTC)
receipt = await slices.record(
    "讨论了明天的演示，决定先完成切片层接入。",
    producer_event_id="glasses-segment-001",
    occurred_at=occurred_at,
    metadata={"evidence_ref": "local:recording-001"},
)
assert receipt.job_id is None

# 一般对话不等待压缩；需要确认投影已落盘时显式等待。
await scheduler.wait_for_slices(timeout=30)
status = store.get_slice_status(receipt.event.event_id, user_id="user")
assert status.status == "completed"
```

原始切片接收成功即返回回执，独立处理器压缩完成后，摘要才成为有来源的活动更新。空闲时用 `store.list_activity()` 查看已完成的摘要。下一次主模型请求会把这些摘要和感知变化合并进时间线，`store.history()` 提供已经接纳的会话消息。`wait_for_slices()` 等待所有未完成切片，包括延迟重试。`wait_until_idle()` 只等待主运行与输出投递。

可以在 `events` 下配置切片处理器。

```json
{
  "slices": {
    "enabled": true,
    "timezone": "Asia/Shanghai",
    "strategy": "rule",
    "batch_size": 32,
    "max_input_chars": 12000,
    "max_output_chars": 1200,
    "retry_delay": 5,
    "model_timeout": 30
  }
}
```

`timezone` 用于按 `occurred_at` 划分自然日，默认 `Asia/Shanghai`。迟到切片归到发生当天的切片流，压缩完成后再以独立消息加入会话。用户问“今天会议定了什么”时，可据活动的发生时间筛选，迟到的昨日摘要仍然保留自己的日期。已生成的切片条目保留原时区。

`rule` 默认无需模型：规范空白、合并批内连续重复观察、提取并在额度内截断，截断会保留标记；中间发生过其他活动的重复文本仍保留先后顺序。它是有损文本摘录，不提供语义总结；完整原文始终可查询。`model` 使用独立、无工具的模型请求总结同批切片，不继承主对话历史。`ConversationService` 为文字切片的模型摘要提供主模型客户端。自行调用 `create_scheduler()` 时，文字切片模型摘要使用 `slice_model` 参数，截图描述使用独立的 `image_model` 参数。两种策略都受单批输入与单条输出字符限制，超长原始切片不会直接塞进模型上下文。模型或自定义压缩失败时保留原始事件，记录错误类型和重试时间，后续重试；主会话仍可继续。调度器关闭会取消尚未提交的压缩，重启后继续处理未完成事件。

通过 `wintermute.events.slices` 的 `SliceCompressor` 协议，可以实现 `strategy` 标识及 `async compress(events, *, max_chars) -> str`，并注入 `SliceProcessor(..., compressor=...)`，最后传给 `RunScheduler(..., slice_processor=processor)`。该压缩接口独立于主/子 Agent 的对话摘要器。配置 `events.slices.enabled=false` 可以停止自动处理，原始切片仍会保存。直接构造调度器时可传 `slice_processor=False`。

模型摘要请求使用配置时区的发生时间，并明确给出当地日期和时区，要求摘要正文标注时间范围。原文中的“今天／昨天”以该条观察的发生时间为参照，无法确定时保留不确定性。

`store.list_slice_stream(user_id="user", conversation_id="work", day="2026-10-29", timezone="Asia/Shanghai")` 查询指定日期的共享及本会话切片，按发生时间从近到远返回。

`store.list_activity(user_id="user", conversation_id="work")` 按观察发生时间列出该会话收到的变化。要查看主 Agent 接纳这些消息的次序，可以从会话历史读取活动正文。

```python
from wintermute.agent import UserMessage

for message in store.history("work", "user"):
    if isinstance(message, UserMessage) and message.source == "timeline":
        print(message.model_text())
```

感知活动的正文说明观察的对象、状态和值，切片活动的正文是已提交的摘要。需要追查摘要依据时，从 `store.list_activity()` 返回的条目读取 `activity_id`、`source_event_ids`、`occurred_at` 和 `known_at`；原始音频和逐段转录保存在切片存储中。

无需密钥的完整示例会演示切片入库、重复发送去重、按发生日期查询切片流、空闲时静默追加摘要，以及下一次对话从历史回答当天的问题：

```bash
uv run python examples/slice_stream.py
```

## 模型、工具和事件契约

- **模型**：实现 `stream(context) -> AsyncIterator[ModelEvent]`，先产生零到多个 `TextDelta`，最后恰好一个完整的 `AssistantMessage`。直接调用 `run()` 时，只有完整消息进入历史，工具参数只从最终消息读取。流中断、截断、缺失最终消息、重复工具 ID 等会终止当前运行。
- **工具**：异步执行，按模型声明顺序调用。Pydantic 参数使用 `strict=True` 验证；是否拒绝额外字段由参数模型的 `extra` 配置决定。未知工具、参数错误、执行异常和超时会生成 `is_error=True` 的工具结果，再交给模型处理。
- **取消**：传播到正在等待的模型或工具；已经完成的消息保留，未返回结果的工具调用补上取消结果，保持历史协议完整。取消不能回滚已发生的工具副作用；工具实现必须正确传播取消，不能阻塞事件循环。
- **上下文**：可注入异步 `transform_context(messages)`，随后由可选的 `context_manager` 执行长度检查和自动摘要，不改完整历史。变换后及压缩后的历史都必须保持 tool call/result 配对。
- **边界**：默认每次运行最多 20 个模型轮次，超出抛 `MaxTurnsExceeded`。模型异常向调用方传播，并记录在 `state.error`；可通过 `continue_run()` 重试。尚未消费的队列保留。
- **运行事件**包括 `agent_start/end`、`turn_start/end`、`turn_interrupted`、`message_start/update/end`、`tool_execution_start/end`、`context_prepared` 和 `error`。`message_update.delta` 为文本增量。`turn_interrupted` 标识被新输入打断的模型轮次。`agent_end.status` 为 completed/cancelled/error。出错或取消时直接发终止事件，未完成的 message/turn 不保证配对。监听器按注册顺序等待，普通异常记录日志并隔离。监听器应尽快返回，不能等待同一 runner 空闲。

核心不依赖特定模型 SDK；OpenAI SDK 是可选依赖。父 Runner 的工具调用仍按顺序执行，`subagent` 内部可并行运行独立子任务。事件、调度和输出持久化由 `wintermute.events` 提供；自动重放失败的模型运行、通用并行工具调度、供应商 token 用量统计和网络认证仍需上层扩展。

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
    __init__.py     # 四层事件入口、存储和运行层公共接口
    core/
      __init__.py   # 事件协议和公共类型导出
      types.py      # 事件信封、终端、接收回执、状态和异常
      slices.py     # 当日切片流条目及原始切片的处理状态
    ingress/
      __init__.py   # 四个入口及终端绑定接口
      gateway.py    # 多终端绑定、统一接收、校验和路由
    storage/
      __init__.py   # 存储实现公共入口
      sqlite.py     # SQLite 分层事件、状态、调度租约和输出记录
    runtime/
      __init__.py   # 上下文组装、调度和投递公共接口
      context.py    # 读取统一会话历史与本次触发输入
      scheduler.py  # 持久队列、会话调度、恢复和流式事件
      session.py    # 将一次执行绑定到持久化输入、工具边界及回复终端
      output.py     # 终端输出、确认及失败重试
    integrations/
      __init__.py   # 外部模块与事件系统的集成接口
      subagent.py   # 后台子 Agent 结果发布桥接
    slices/
      __init__.py   # 独立切片处理器及可替换压缩接口
      processor.py  # 分组、输入分批、每日投影及失败重试
      compression.py # 规则摘录、无工具模型摘要及压缩协议
      audio.py      # 完整转录缓存及分级音频摘要
      asr.py        # DeepSeek 语音识别客户端
      asr_audio.py  # 录音直传与超限音频的 VAD 分段
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
    http.py         # HTTP 认证、交流、感知与文字切片接入、任务查询和输出确认
    audio.py        # Ogg 上传、完整性校验、持久化与处理状态查询
    telegram.py     # Telegram 白名单、长轮询与回复投递
    service.py      # 按配置启动接口、会话调度和后台通知
config/
  settings.example.json   # 可提交的配置模板，不含真实密钥
  interfaces.example.json # HTTP 服务及 Telegram 绑定配置模板
  audio.example.json      # 设备上传、独立 ASR 与音频总结模型配置
examples/
  mock_agent.py     # 离线工具闭环
  ark_agent.py      # 火山方舟真实接口示例
  builtin_tools.py  # 临时目录中的四种基础工具演示
  ark_tools.py      # 带本地工具的火山方舟 Agent
  subagents.py      # 子 Agent 并行执行与真实文件读取的离线演示
  background_subagents.py  # 后台读取、主对话继续响应和自动通知的离线演示
  context_management.py    # 主/子 Agent 独立预算与自动摘要的离线演示
  event_runtime.py         # 多终端、统一会话历史、调度与后台通知的离线演示
  meeting_interruptions.py # 会议中用户引导和后台结果在生成或调用工具时插入
  slice_stream.py          # 切片入库、当日流、重试去重与上下文接入的离线演示
  configuration.py         # JSON 配置、事件运行和日志的离线演示
  configured_agent.py      # 从 JSON 启动真实模型、主/子 Agent 和事件运行层
tests/             # 默认全部离线，无 API Key 依赖
```

Agent 相关模块统一从 `wintermute.agent` 下导入。运行核心仍可使用 `from wintermute.agent import AgentRunner, Tool`；上下文、模型适配器和工具分别使用 `wintermute.agent.context`、`wintermute.agent.providers` 和 `wintermute.agent.tools`。需要按类别导入时，可使用 `wintermute.agent.tools.builtin`、`wintermute.agent.tools.subagent` 或 `wintermute.agent.providers.openai`。原顶层的 `wintermute.context`、`wintermute.providers` 和 `wintermute.tools` 导入路径已迁移；事件接入与调度继续使用 `wintermute.events`。

事件模块按职责分为 `core`（事件协议）、`ingress`（接入）、`storage`（存储）、`runtime`（组装、调度与投递）、`slices`（切片处理与压缩）和 `integrations`（模块集成）。`from wintermute.events import EventGateway, SQLiteStore, RunScheduler` 等公共导入保持不变，也可从对应子包按职责导入。

## 开发验证

```bash
uv sync --locked --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

安装开发与可选依赖后，可运行 `uv run pytest`。仓库保留了会议、上下文压缩和终端采集等回归测试，其他本地离线测试由 `.gitignore` 排除。

CI 配置覆盖 Windows / Ubuntu 和 Python 3.12、3.13、3.14。`uv.lock` 锁定开发与可选依赖；构建的 wheel 包含类型标记 `py.typed`。

## 设计参考

参考 [pi agent-core](https://github.com/earendil-works/pi/tree/89c921d8b56b59e768b26e7f48b6832978119310/packages/agent) 的状态包装器、模型/工具循环、事件和 steering / follow-up 分层，以独立 Python 实现适配 Wintermute。参考版本固定为 `89c921d8b56b59e768b26e7f48b6832978119310`，并非完整 API 移植。

当前实现使用顺序工具、严格输入校验、Python 标准取消语义和显式轮数上限。四层事件运行是 Wintermute 自身的应用层设计；平台接入、长期记忆、人格与更复杂的注意力策略仍将逐步构建。

基础工具参考 pi 的 [read / write / edit / bash / powershell 实现](https://github.com/earendil-works/pi/tree/89c921d8b56b59e768b26e7f48b6832978119310/packages/coding-agent/src/core/tools)：采用分页读取、批量精确编辑、输出截断及命令取消等行为；这里用 `exec` 统一命令工具名称，编辑参数使用 Python 风格的 `old_text` / `new_text`，不移植图片读取、模糊编辑和后台任务功能。

子 Agent 工具参考 pi 的 [subagent 扩展示例](https://github.com/earendil-works/pi/tree/main/packages/coding-agent/examples/extensions/subagent)：采用显式角色配置、独立上下文、单任务与并行委派、事件转发和取消传播；这里使用 `asyncio` 与独立 `AgentRunner` 实例实现，不启动 pi 子进程，也不依赖 tmux。

上下文管理参考 pi 的[自动压缩与安全切分](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/compaction/compaction.ts)和[摘要序列化](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/compaction/utils.ts)：采用预留输出空间、保留近期消息、增量更新摘要及完整工具调用边界。这里为主/子 Agent 提供独立管理入口，并给摘要请求增加自身的输入预算；不移植 pi 的会话文件、usage 校准和超窗恢复重试。
