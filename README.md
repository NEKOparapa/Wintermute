# Wintermute

一个逐渐理解你的个人 AI 执事。项目以可复用的 `AgentRunner` 和分层事件运行层为基础，后续继续完善长期记忆、人格和更多设备。

当前已实现 Python 异步 Agent 核心、文件与命令基础工具、前台和后台子 Agent、主/子 Agent 上下文管理、支持多设备和 SQLite 持久化的五层事件入口、按采集时间记录的切片流，以及感知和切片共同参与的合并线压缩、分目录记忆档案与独立后台记忆 Agent、历史记录导入、可配置的 Telegram 与 HTTP 交流接口、JSON 配置与结构化日志，以及 OpenAI 兼容 Responses / Chat Completions 适配器。默认使用 **Responses API**，保留异步流式输出和工具调用。项目由 [uv](https://docs.astral.sh/uv/) 管理，要求 **Python >= 3.12**，默认开发版本为 3.12。

## 快速开始

首次运行时，将 `config/interfaces.example.json` 复制为 `config/settings.json`，按其中的 `api_key_env` 和 `token_env` 设置模型及设备凭据。

```bash
uv sync --locked --extra openai --extra interfaces
uv run python -m wintermute --config config/settings.json
```

用户档案可在桌面端「记忆 → 用户档案」查看。手工维护档案时，偏好注明适用场景，习惯注明观察范围，近期状态注明截止或复核日期。

## Agent 工具

工具实现集中在 [agent/tools](src/wintermute/agent/tools)。主会话的工具组合由 [registry.py](src/wintermute/agent/tools/registry.py) 按会话、记忆库和设备绑定创建。

| 工具目录 | 工具职责 |
| --- | --- |
| `builtin/` | 文件读取、写入、编辑和命令执行。主 Agent 和子 Agent 分别使用各自的 `tools.enabled` 配置。 |
| `conversation/` | `schedule_read` 查询会话日程，`schedule_write` 创建、修改和删除日程。 |
| `calendar/` | `time_query` 在本地读取或换算时间，`calendar_query` 查询中国大陆放假安排、调休补班和工作日。 |
| `devices/` | `device_statuses` 查询设备名称、设备 ID、平台、在线状态和本会话能否向其发送通知。`device_messaging` 发送定向通知或会话提问。 |
| `web/` | `web_search` 搜索互联网并返回来源链接和摘要，`web_fetch` 读取指定网页的 Markdown 正文。 |
| `connectors/` | `listing.py` 查询连接器，`setup.py` 登记服务，`credentials.py` 保存凭据，`mcp.py` 发现并调用工具。设备连接器的参数、任务队列和回执也在此目录。 |
| `memory/` | `read.py` 查询记忆，`write.py` 更新记忆，`parameters.py` 定义参数。主会话和后台记忆 Agent 共用这组工具，写入权限按调用方传入。 |
| `subagent/` | `subagent_status` 查询后台任务和结果，`subagent_control` 启动或取消任务。 |

`schedule_read({"operation":"list"})` 分页列出本会话日程，返回当前时间和时区。`schedule_read({"operation":"get","id":"<日程 ID>"})` 读取完整正文。`schedule_write` 使用 `create`、`update` 或 `delete` 操作。创建时提供标题和带时区的触发时间，修改或删除时使用日程 ID。到点后，主 Agent 会收到日程通知。

`time_query({})` 返回配置时区的当前时间。用 `{"action":"convert","at":"2026-11-20T09:00:00","from_timezone":"Asia/Shanghai","timezone":"America/New_York"}` 在本地换算时区。遇到夏令时结束造成的重复时刻，可在 `at` 中传入明确的 UTC 偏移。

`calendar_query` 使用北京时间。用 `{"action":"day","date":"2026-01-04"}` 查一天，或用 `{"action":"month","month":"2026-01"}` 查整月。要创建“下一个工作日上午九点”的提醒，先传 `{"action":"workday_offset","offset":1,"at_time":"09:00"}`，再将返回的 `trigger_at` 原样传给 `schedule_write`。正数向后、负数向前，都从起点的相邻日期算起。

后端按公告年份在 SQLite 中缓存数据，每 24 小时按需刷新，离线时可使用上一次有效的缓存。12 月也检查下一年度公告中的跨年调整。若缺少判定需要的数据，日历返回日期和星期，并将工作日标为未知；工作日推算会指出无法确认的日期。[holiday-cn 数据说明](https://github.com/NateScarlet/holiday-cn)

`subagent_control({"action":"start","agent":"reader","task":"总结 README.md"})` 立即返回任务 ID。也可以传 `tasks` 数组，一次启动最多 8 个独立任务。`subagent_status({})` 列出本会话保留的任务，传 `task_id` 可查看单个任务的状态和结果。用 `subagent_control({"action":"cancel","task_id":"<任务 ID>"})` 请求取消。子任务结束后，主会话会自动收到通知。

`ConnectorList({})` 的 `devices` 列出本会话绑定的每台设备。设备的 `status` 为 `online`、`offline` 或 `unknown`，`tools` 是最近上报的工具。每台设备的 `connectors` 分别列出连接器的启用设置（`enabled`）、可用状态（`connected`、`status`）、授权方式（`authMode`）和验证结果（`verified`）。账号级连接器列在 `accountConnectors`，其中 `connections` 列出各账号的验证结果。

`ConnectorList({"id":"obsidian"})` 返回每台装有 Obsidian 的设备。加上 `"device_id":"desktop"` 可缩小到该设备。单独传 `device_id` 可列出该设备上的全部连接器。指定设备时，`accountConnectors` 为空。

检查权限时，向 `ConnectorList` 传入连接器 ID 和本次操作需要的权限。例如，`{"id":"slack","requestedGrants":{"bot":["chat:write"],"user":[]}}` 会在 `missingGrants` 中按 bot、user 返回与本地授权记录相比缺少的权限。OAuth 权限用 `scopes` 查询，差额列在 `missingScopes`。缺少所需权限时，该项的 `connected` 为 `false`。

`Mcp` 接受一个对象，支持 `search`、`list`、`describe`、`call` 四个动作。`search` 和 `ConnectorList` 返回可直接使用的 `provider`，设备实例采用 `连接器ID@设备 ID`。同一连接器只安装在一台设备时也可传原始连接器 ID。若安装在多台设备上，只传原始 ID 会返回供选择的完整标识。`connectionId` 用于选择同一 MCP 服务下的账号。

例如，搜索得到 `obsidian@desktop` 后，传入 `{"action":"describe","provider":"obsidian@desktop","tool":"obsidian"}` 查看参数，再通过 `{"action":"call","provider":"obsidian@desktop","tool":"obsidian","args":{"action":"vault_info"}}` 查询该电脑的笔记库。设备调用先返回含调用 ID 的排队回执，客户端执行后将结果送回原会话。调用参数校验失败会返回 `inputSchema`。

`device_statuses({})` 列出当前账号已连接过的设备，也包括本会话已配置但尚未上线的通知目标。每项返回 `device_id`、`name`、`platform`、`status`（`online`、`offline` 或 `unknown`）和 `can_notify`。设备通过 `X-Device-Platform` 上报 `windows`、`macos`、`android`、`ios` 或 `esp32`，尚未上报的设备返回 `null`。向指定设备发送通知时，从 `can_notify` 为 `true` 的设备中选择，例如 `device_messaging({"action":"notify","targets":["desktop"],"title":"报告已完成","body":"可以查看报告了。"})`。离线设备上线后可读取收件箱。

需要用户答复时，调用 `device_messaging({"action":"ask","title":"确认时间","question":"周几安排评审？","options":["周一","周二"]})`。提问在本会话各设备上可见，任一设备回答或忽略后，主 Agent 会收到结果。

读取 Android 或 ESP32 的运行数据时，客户端需先启用并上报 `device_status`，再由 `Mcp` 搜索该工具，使用返回的设备专属 `provider` 和空参数 `{}` 调用。需要核对进度或取回原结果时，向 `Mcp` 传入 `{"action":"call","provider":"wintermute:receipts","tool":"get_call_result","args":{"id":"调用 ID"}}`。回执保存在原会话，设备离线后也可查阅。

主 Agent 默认启用 `read`、`write`、`edit`、`exec`，可通过 `tools.enabled` 选择要开放的文件和命令工具。`exec` 在后端机器执行。子 Agent 单独配置。

主会话的联网工具默认启用。设置后端环境变量 `DEEPSEEK_API_KEY`，或在设置页的 API Key 管理中填写标有「网络搜索与网页读取」的凭据。聊天模型可使用其他提供方。可通过 `tools.web.api_key_env` 指定独立环境变量，或用 `tools.web.enabled: false` 关闭这两个工具。

`web_search({"query":"site:api-docs.deepseek.com 网页抓取","count":5})` 返回匹配网页及摘要，设置 `need_content` 可随结果取得可用正文。已知链接时可直接调用 `web_fetch({"url":"https://api-docs.deepseek.com/zh-cn/guides/search/"})`，结果包含来源 URL、标题和 Markdown 正文。后端调用 DeepSeek 的独立 Search、Fetch 接口，产生有效结果的请求按次计费。[DeepSeek 网页搜索与抓取文档](https://api-docs.deepseek.com/zh-cn/guides/search/)

桌面和手机首次安装时生成持久设备 ID，ESP32 默认从出厂 MAC 生成。将各客户端显示的 ID 分别登记在 `interfaces.http.devices`，设置对应的用户、会话、设备类型和令牌。升级时可继续使用已配置的 ID。每次请求使用 `X-Device-ID` 和 Bearer 令牌认证；客户端通过 `X-Device-Instance` 标记安装实例，以 Base64 编码的 UTF-8 在 `X-Device-Name` 上报名称。`POST /v1/devices/self` 保存新名称。已有安装绑定的 ID 被另一安装复用时，后端返回 `409 device_id_in_use`。

后端配置和接口使用 `device_id`，桌面和移动客户端代码使用 `deviceId`。升级时后端自动迁移 SQLite 中的设备字段，旧配置仍可读取。先更新后端，再更新各客户端；过渡期间后端也接受旧固件发送的 `X-Terminal-ID`，每个请求只能使用一个设备身份请求头。

## 记忆档案与历史记录

在 `events.memory.user_dirs` 中为每个用户配置独立目录（路径相对配置文件）。示例配置将 `local` 用户映射到 `data/memory/local`。目录不能彼此包含或共享。启用后，`agent-character/` 保存角色约定，`about-user/` 保存用户画像，`note/` 保存长期笔记，`log/` 自动记录修改；删除的文件保留在 `.trash/`。

编辑包内的 Markdown 资源即可调整默认内容。每个账号首次初始化时，后端将模板复制到对应的空记忆区。[角色模板](src/wintermute/memory/templates/agent-character/character.md) · [用户模板](src/wintermute/memory/templates/about-user/profile.md)

主会话会读取角色、画像和自动生成的笔记索引，并通过 `memory_read` / `memory_write` 按需查阅和更新记忆。后台记忆 Agent 按账号计划整理累计的对话和导入记录，并维护记忆。模型可用 `events.memory.model` 单独配置，默认使用主模型。索引和提示词有独立的字节预算，超限时截断，后台在计划执行时优先精简。

桌面端和移动端的「设置 → 记忆整理」可设定间隔天数和运行时间，并查看下次整理时间。默认每天 03:00，时区取自 `events.slices.timezone`。新计划从最近一次尚未到达的设定时刻开始，之后每隔指定的日历天数运行。服务停机错过多次时，恢复后补跑一次，沿原计划继续。时间到了而主会话仍在处理请求时，整理会等到请求结束。

记忆配置统一使用 `events.memory`。旧的 `profile_files`、`profile_dreaming` 配置和单文件整理流程已移除，请使用更新后的示例配置。

后端按应用组装、设备接口、运行编排、领域规则和持久化分层。音频摘要模型统一配置在 `events.timeline_compression.summary_model`；已移除无效的切片 `strategy`、`max_input_chars` 和 `model_timeout` 配置。

HTTP 接口沿用 `X-Device-ID` 和 `Authorization: Bearer ...` 认证，仅访问当前设备所属用户：

| 接口 | 用途 |
| --- | --- |
| `POST /v1/transcriptions` | 请求体为 `audio/wav`，16 kHz 单声道、16 位 PCM，最长 120 秒。返回 `{"text":"..."}` 供客户端编辑。 |
| `POST /v1/chat/files?name=文件名` | 上传原始字节（`application/octet-stream`，最多 32 MiB），返回本会话的 `{id,name}`。 |
| `POST /v1/messages` | 提交 `message_id`、`text` 和可选的 `documents: [{id,name}]`。图文混合消息可在 `/v1/messages/images` 使用相同的附件引用。 |
| `POST /v1/messages/images` | 提交 `message_id`、`text` 和 `images`。每条消息支持 1–4 张 JPEG data URL，每张解码后最多 512 KiB。重试时复用同一 ID 和图片顺序。 |
| `GET /v1/memory/tree` | 文件清单及笔记标题、摘要 |
| `GET /v1/memory/schedule` | 当前账号的间隔天数、运行时间、时区和下次整理时间 |
| `POST /v1/memory/schedule` | 保存计划，例如 `{"interval_days":3,"local_time":"02:30"}` |
| `GET /v1/memory/file?path=note/example.md` | 完整正文和 `content_sha256` |
| `PUT /v1/memory/file?path=note/example.md` | JSON `{ "content": "...", "reason": "手动更新", "expected_hash": "..." }`；新建可省略哈希 |
| `DELETE /v1/memory/file?path=note/example.md` | 软删除，可附带 `expected_hash` |
| `GET /v1/memory/logs?date=2026-11-19` | 当日审计日志 |
| `POST /v1/records/batches` | 导入历史记录，最多 16 MiB |
| `GET /v1/records/batches/{batch_id}` | 查询处理、过滤和失败计数 |
| `GET /v1/records/items` | 按原始发生时间倒序分页；支持 `record_type`、`limit`、`cursor` |
| `GET /v1/records/items/{item_id}` | 分段读取原文；`offset` 按 Unicode 字符计数，`limit` 默认 16000、最多 64000 |

聊天语音使用 `events.slices.audio.asr` 配置，默认从 `DEEPSEEK_API_KEY` 读取密钥，可独立于后台音频采集使用。图片随对话发给配置的主模型。使用 DeepSeek 时，主模型可设为 `deepseek-flash`，服务地址为 `https://api.deepseek.com`。[语音识别](https://api-docs.deepseek.com/zh-cn/guides/asr/) · [图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision/)

安装 HTTP 接口依赖时会一并安装 MarkItDown 的本地文档转换器。上传文件在独立进程解析，音频附件使用同一个 ASR 服务。后端按用户、会话、内容和扩展名缓存原文件与 Markdown，支持重试和重启后复用。每个文档的文件名和完整 Markdown 会随消息进入聊天上下文，后续追问可复用同一份正文。应用不按字符数截断文档，整轮输入仍受所用模型的上下文窗口限制。会话历史的 `files` 字段提供原文件下载 ID。[MarkItDown 支持的格式](https://github.com/microsoft/markitdown)

记录批次支持 `sms`、`call_log`、`contacts`、`notion`、`recording_list`、`document`。每批包含 `record_type`、`source_name` 和 `items`；每条包含带时区的 `occurred_at`、`content`（字符串或结构化对象），以及可选 `external_id`、`title`。可选的 `batch_id` 用于重试同一请求；相同来源的重复内容不会重复整理。大载荷单独落盘，验证码和广告短信先过滤，历史资料不会进入实时活动时间线或创建主会话任务。

```json
{
  "record_type": "document",
  "source_name": "personal-notes",
  "items": [{
    "external_id": "project-egret",
    "occurred_at": "2026-11-19T09:00:00+08:00",
    "title": "白鹭项目",
    "content": {"text": "每周三下午三点召开项目例会。"}
  }]
}
```

所有人工和 Agent 写入均经过路径、权限、凭据检测和审计。程序维护 `log/`，记录整理只能修改用户画像和笔记。可选 `git_snapshot_enabled` 会在记忆目录内保存本地版本快照，默认关闭。

笔记支持严格的 Markdown Frontmatter：`title`、`summary`、`tags`、`status`、`updated_at`、`review_at`、`expires_at`、`sources`。归档和过期笔记不进入常驻索引，仍可搜索。`about-user` 文件的 `expires_at` 和单行末尾的 `[expires_at: YYYY-MM-DD]` 可标注近期状态期限，过期后不再注入主会话。日期按 `events.slices.timezone` 计算。

开发验证：安装 `interfaces` 可选依赖后运行 `uv run ruff check src tests`、`uv run mypy`、`uv run pytest`。测试使用临时目录、模拟模型和本机 HTTP 服务，不需要模型 API 密钥。
