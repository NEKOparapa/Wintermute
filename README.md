# Wintermute
个人AI结合体

## 启动服务

```powershell
uv run python -m app.server
```

## 模型接口

底层使用 OpenAI **Responses API**（`/responses`）调用兼容服务。系统提示通过
`instructions` 传入，对话历史通过 `input` 项列表传入，工具走原生 function 协议。
在 `config/settings.json` 中配置：

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "model": "gpt-4o"
}
```

## HTTP 接口

健康检查：

```powershell
Invoke-RestMethod -Method Get http://127.0.0.1:8000/health
```

发送一条消息：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/event `
  -ContentType "application/json" `
  -Body '{"message":"你好"}'
```

### 注意力层级

`/event` 通过 `level` 选择处理链路：

- `L0`：用户主动对话，默认 `type=user_message`。
- `L1`：外部事件主动唤醒，默认 `type=l1_trigger`，走独立 L1 prompt。
- `L2` / `L3`：背景事件，默认 `type=observation`，只落库并压缩进事件记忆。

L1 示例：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/event `
  -ContentType "application/json" `
  -Body '{"level":"L1","source":"calendar","message":"15:00 有牙医预约，距离出门时间不足 30 分钟。"}'
```

L1 处理结果会写入当天共享上下文 `data/memories/l1_context/YYYY-MM-DD.json`。
L0 后续对话会读取这份摘要，但不会读取 L1 原始处理链路。

### 多模态输入

`/event` 支持随消息携带 `attachments`，用于图片 / 音频 / 视频 / 文件输入。
`message` 与 `attachments` 至少要有一个非空。每条附件用 `kind` 区分类型：

```json
{
  "message": "看看这张图，再听听这段录音",
  "attachments": [
    { "kind": "image", "url": "https://example.com/cat.png", "detail": "high" },
    { "kind": "image", "data": "<base64>", "mime_type": "image/png" },
    { "kind": "audio", "url": "https://example.com/voice.mp3" },
    { "kind": "audio", "data": "<base64>", "mime_type": "audio/mpeg" },
    { "kind": "file",  "data": "<base64>", "filename": "doc.pdf", "mime_type": "application/pdf" },
    { "kind": "video", "url": "https://example.com/clip.mp4" },
    { "kind": "video", "path": "D:/Videos/demo.mp4" }
  ]
}
```

附件字段说明：

- `kind`：`image` / `audio` / `video` / `file`（必填）。
- 内容来源（至少一个）：`url`（远程地址或 data URL）、`data`（base64 原文）、
  `file_id`（已上传到服务端的文件 ID）、`path`（服务本机可读的本地文件路径）、
  `content_part`（直通原始 content part）。
- `mime_type`：配合 `data` 拼装 data URL。
- `format`：音频格式，`mp3` 或 `wav`；未提供 `mime_type` 时用于推断音频 data URL 的 MIME。
- `filename`：文件名（`file` 类型用 base64 时建议提供）。
- `detail`：图片细节；火山方舟 Responses API 当前支持 `low` / `high` / `xhigh`。
- `preprocess_configs`：本地 `path` 上传时透传给 Files API，例如
  `{ "video": { "fps": 0.3 } }`。

说明：

- 图片、音频、视频、文件按火山方舟 Responses API 的 `input_*` content part 结构构造。
- 使用 `path` 时，服务会先调用 Files API 上传并等待文件处理完成，再把返回的
  `file_id` 写入事件历史。
- 如兼容服务要求特殊字段，可用 `content_part` 直通该服务要求的结构。

对话历史会自动按日期写入 `data/events/YYYY-MM-DD.json`，分层记忆写入 `data/memories/`。
附件信息随用户事件保存在 `metadata.attachments` 中。

## Telegram Webhook 网关

Telegram 接入使用独立进程，不改变主服务对话链路。先复制配置模板：

```powershell
Copy-Item config/telegram.example.json config/telegram.json
```

填写 `bot_token`、`webhook_url`、`webhook_secret_token` 和 `allowed_chat_ids` 后，先启动
Wintermute 主服务，再启动 Telegram 网关：

```powershell
uv run python -m app.server
uv run python -m app.telegram_gateway --config config/telegram.json
```

网关会在启动时自动调用 Telegram `setWebhook`。`webhook_url` 需要是公网 HTTPS
地址，并转发到网关本地监听的 `host` / `port` / `path`。
