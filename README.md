# Wintermute

一个逐渐理解你的个人 AI 执事。项目以可复用的 `AgentRunner` 和分层事件运行层为基础，后续继续完善长期记忆、人格和更多终端。

当前已实现 Python 异步 Agent 核心、文件与命令基础工具、前台和后台子 Agent、主/子 Agent 上下文管理、支持多终端和 SQLite 持久化的五层事件入口、按采集时间记录的切片流，以及感知和切片共同参与的合并线压缩、分目录记忆档案与独立后台记忆 Agent、历史记录导入、可配置的 Telegram 与 HTTP 交流接口、JSON 配置与结构化日志，以及 OpenAI 兼容 Responses / Chat Completions 适配器。默认使用 **Responses API**，保留异步流式输出和工具调用。项目由 [uv](https://docs.astral.sh/uv/) 管理，要求 **Python >= 3.12**，默认开发版本为 3.12。

## 快速开始

首次运行时，将 `config/interfaces.example.json` 复制为 `config/settings.json`，按其中的 `api_key_env` 和 `token_env` 设置模型及终端凭据。

```bash
uv sync --locked --extra openai --extra interfaces
uv run python -m wintermute --config config/settings.json
```

用户档案可在桌面端「记忆 → 用户档案」查看。手工维护档案时，偏好注明适用场景，习惯注明观察范围，近期状态注明截止或复核日期。

## 记忆档案与历史记录

在 `events.memory.user_dirs` 中为每个用户配置独立目录（路径相对配置文件）。示例配置将 `local` 用户映射到 `data/memory/local`。目录不能彼此包含或共享。启用后，`agent-character/` 保存角色约定，`about-user/` 保存用户画像，`note/` 保存长期笔记，`log/` 自动记录修改；删除的文件保留在 `.trash/`。

主会话会读取角色、画像和自动生成的笔记索引，并通过 `memory_read` / `memory_write` 按需查阅和更新记忆。后台记忆 Agent 仅使用这两个工具，在主会话空闲时整理对话和历史记录。模型可用 `events.memory.model` 单独配置，默认使用主模型。索引和提示词有独立的字节预算，超限时截断并触发后台精简。

记忆配置统一使用 `events.memory`。旧的 `profile_files`、`profile_dreaming` 配置和单文件整理流程已移除，请使用更新后的示例配置。

后端按应用组装、终端接口、运行编排、领域规则和持久化分层。音频摘要模型统一配置在 `events.timeline_compression.summary_model`；已移除无效的切片 `strategy`、`max_input_chars` 和 `model_timeout` 配置。

HTTP 接口沿用 `X-Terminal-ID` 和 `Authorization: Bearer ...` 认证，仅访问当前终端所属用户：

| 接口 | 用途 |
| --- | --- |
| `POST /v1/transcriptions` | 请求体为 `audio/wav`，16 kHz 单声道、16 位 PCM，最长 120 秒。返回 `{"text":"..."}` 供客户端编辑。 |
| `POST /v1/chat/files?name=文件名` | 上传原始字节（`application/octet-stream`，最多 32 MiB），返回本会话的 `{id,name}`。 |
| `POST /v1/messages` | 提交 `message_id`、`text` 和可选的 `documents: [{id,name}]`。图文混合消息可在 `/v1/messages/images` 使用相同的附件引用。 |
| `POST /v1/messages/images` | 提交 `message_id`、`text` 和 `images`。每条消息支持 1–4 张 JPEG data URL，每张解码后最多 512 KiB。重试时复用同一 ID 和图片顺序。 |
| `GET /v1/memory/tree` | 文件清单及笔记标题、摘要 |
| `GET /v1/memory/file?path=note/example.md` | 完整正文和 `content_sha256` |
| `PUT /v1/memory/file?path=note/example.md` | JSON `{ "content": "...", "reason": "手动更新", "expected_hash": "..." }`；新建可省略哈希 |
| `DELETE /v1/memory/file?path=note/example.md` | 软删除，可附带 `expected_hash` |
| `GET /v1/memory/logs?date=2026-11-19` | 当日审计日志 |
| `POST /v1/records/batches` | 导入历史记录，最多 16 MiB |
| `GET /v1/records/batches/{batch_id}` | 查询处理、过滤和失败计数 |
| `GET /v1/records/items` | 按原始发生时间倒序分页；支持 `record_type`、`limit`、`cursor` |
| `GET /v1/records/items/{item_id}` | 分段读取原文；`offset` 按 Unicode 字符计数，`limit` 默认 16000、最多 64000 |

聊天语音使用 `events.slices.audio.asr` 配置，默认从 `DEEPSEEK_API_KEY` 读取密钥，可独立于后台音频采集使用。图片随对话发给配置的主模型。使用 DeepSeek 时，主模型可设为 `deepseek-flash`，服务地址为 `https://api.deepseek.com`。[语音识别](https://api-docs.deepseek.com/zh-cn/guides/asr/) · [图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision/)

安装 HTTP 接口依赖时会一并安装 MarkItDown 的本地文档转换器。上传文件在独立进程解析，音频附件使用同一个 ASR 服务。单个附件的 Markdown 最大 8 MiB。后端按用户、会话、内容和扩展名缓存原文件与 Markdown，支持重试和重启后复用。主 Agent 首次看到文件名及开头 2,000 字符，需要全文时可用 `read_chat_file` 分段读取。会话历史的 `files` 字段提供原文件下载 ID。[MarkItDown 支持的格式](https://github.com/microsoft/markitdown)

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

所有人工和 Agent 写入均经过路径、权限、凭据检测和审计。`activity.md` 与 `log/` 由程序维护；记录整理不能修改角色设定。可选 `git_snapshot_enabled` 会在记忆目录内保存本地版本快照，默认关闭。

笔记支持严格的 Markdown Frontmatter：`title`、`summary`、`tags`、`status`、`updated_at`、`review_at`、`expires_at`、`sources`。归档和过期笔记不进入常驻索引，仍可搜索。`about-user` 文件的 `expires_at` 和单行末尾的 `[expires_at: YYYY-MM-DD]` 可标注近期状态期限，过期后不再注入主会话。日期按 `events.slices.timezone` 计算。

开发验证：安装 `interfaces` 可选依赖后运行 `uv run ruff check src tests`、`uv run mypy`、`uv run pytest`。测试使用临时目录、模拟模型和本机 HTTP 服务，不需要模型 API 密钥。
