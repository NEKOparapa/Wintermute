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

### 多模态输入

`/event` 支持随消息携带 `attachments`，用于图片 / 音频 / 视频 / 文件输入。
`message` 与 `attachments` 至少要有一个非空。每条附件用 `kind` 区分类型：

```json
{
  "message": "看看这张图，再听听这段录音",
  "attachments": [
    { "kind": "image", "url": "https://example.com/cat.png", "detail": "auto" },
    { "kind": "image", "data": "<base64>", "mime_type": "image/png" },
    { "kind": "audio", "data": "<base64>", "format": "mp3" },
    { "kind": "file",  "data": "<base64>", "filename": "doc.pdf", "mime_type": "application/pdf" },
    { "kind": "video", "url": "https://example.com/clip.mp4" }
  ]
}
```

附件字段说明：

- `kind`：`image` / `audio` / `video` / `file`（必填）。
- 内容来源（至少一个）：`url`（远程地址或 data URL）、`data`（base64 原文）、
  `file_id`（已上传到服务端的文件 ID）、`content_part`（直通原始 content part）。
- `mime_type`：配合 `data` 拼装 data URL。
- `format`：音频格式，`mp3` 或 `wav`（Responses 的 `input_audio` 仅接受 base64 + 格式）。
- `filename`：文件名（`file` 类型用 base64 时建议提供）。
- `detail`：图片细节，`low` / `high` / `auto` / `original`。

说明：

- 图片、音频、文件由 OpenAI 官方 Responses API 原生支持。
- 视频目前 OpenAI 官方 Responses API 尚未原生支持，需配合支持视频输入的兼容服务
  （如 Gemini / Qwen 兼容端点）；可用 `content_part` 直通该服务要求的结构。

对话历史会自动按日期写入 `data/events/YYYY-MM-DD.json`，分层记忆写入 `data/memories/`。
附件信息随用户事件保存在 `metadata.attachments` 中。
