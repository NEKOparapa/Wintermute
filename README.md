# Wintermute

一个逐渐理解你的个人 AI 执事。项目以可复用的 `AgentRunner` 和分层事件运行层为基础，后续继续完善长期记忆、人格和更多终端。

当前已实现 Python 异步 Agent 核心、文件与命令基础工具、前台和后台子 Agent、主/子 Agent 上下文管理、支持多终端和 SQLite 持久化的四层事件运行、按采集时间记录的切片流，以及感知和切片共同参与的合并线压缩、可配置的 Telegram 与 HTTP 交流接口、JSON 配置与结构化日志，以及 OpenAI 兼容 Responses / Chat Completions 适配器。默认使用 **Responses API**，保留异步流式输出和工具调用。项目由 [uv](https://docs.astral.sh/uv/) 管理，要求 **Python >= 3.12**，默认开发版本为 3.12。

## 快速开始

首次运行时，将 `config/interfaces.example.json` 复制为 `config/settings.json`，按其中的 `api_key_env` 和 `token_env` 设置模型及终端凭据。

```bash
uv sync --locked --extra openai --extra interfaces
uv run python -m wintermute --config config/settings.json
```