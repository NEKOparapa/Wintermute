# Wintermute

<p align="center">
  <img src="assets/wintermute-hero-anime.png" alt="Wintermute 个人 AI 执事形象图" width="720">
</p>

<p align="center">一个逐渐理解你的个人 AI 执事。</p>

## 项目介绍

Wintermute 不是一次性的问答 bot，而是一个长期陪伴你的个人 AI 执事。它会通过对话、录音、视频和更多关于你的数据流，逐渐理解你的语言习惯、长期偏好、正在推进的计划，以及你不希望被越过的边界。

它的目标是安静运行、克制提醒，在合适的时候关心你、提醒你、推动你做出更长期有利的行动；它不应该频繁打扰你，也不应该替你做决定。

当前能力包括：

- 长期记忆与用户画像：把对话和事件压缩成可持续积累的记忆，并更新对用户的稳定理解。
- 统一人格：保留身份、价值观和底线，同时根据长期相处逐渐调整沟通习惯。
- L0/L1/L2/L3 分层事件流：区分用户主动对话、主动提醒和不同优先级的背景事件。
- 日程提醒与工具调用：支持日程管理、文件读写和受控终端工具。

## 未来功能

- 支持 QQ、微信、Discord 等更多平台接入。
- 支持多子代理协作，用于拆分复杂任务和长期计划。
- 支持墨水屏单片机设备，提供更低打扰的常驻显示与提醒入口。

## 环境要求

- Python >= 3.12
- Git
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## 终端部署

### Windows PowerShell

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
git clone https://github.com/NEKOparapa/Wintermute.git
cd Wintermute
uv sync
```

### macOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/NEKOparapa/Wintermute.git
cd Wintermute
uv sync
```

已安装 Homebrew 时，也可以用 `brew install uv` 安装 `uv`。

### Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/NEKOparapa/Wintermute.git
cd Wintermute
uv sync
```

## 项目配置

首次运行前复制配置模板。

### Windows PowerShell

```powershell
Copy-Item config\settings.example.json config\settings.json
Copy-Item config\interfaces\settings.example.json config\interfaces\settings.json
```

### macOS / Linux

```bash
cp config/settings.example.json config/settings.json
cp config/interfaces/settings.example.json config/interfaces/settings.json
```

然后按需修改：

- `config/settings.json`：填写 `base_url`、`api_key`、`model`。
- `config/interfaces/settings.json`：使用 Telegram 时填写 `bot_token`；不使用时将 Telegram 接口的 `enabled` 改为 `false`。

## 启动服务

在项目根目录执行：

```bash
uv run python -m app.server
```
