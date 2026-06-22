# Wintermute

个人AI结合体

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## 项目配置

首次运行前，先复制配置模板并填写实际参数。

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

配置说明：

- `config/settings.json`：填写 `base_url`、`api_key`、`model`，按需调整 `data_dir`、`log_dir`。
- `config/interfaces/settings.json`：使用 Telegram 时填写 `bot_token`，按需设置 `allowed_chat_ids`。
- 不使用 Telegram 时，将 Telegram 接口的 `enabled` 改为 `false`，并清空相关 `flows` 的 `inputs` / `outputs`。

## Windows 终端部署

在 PowerShell 中执行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
cd D:\GitHub\Wintermute
uv sync
uv run python -m app.server
```

## macOS 终端部署

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /path/to/Wintermute
uv sync
uv run python -m app.server
```

也可以使用 Homebrew 安装 `uv`：

```bash
brew install uv
```

## Linux 终端部署

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /path/to/Wintermute
uv sync
uv run python -m app.server
```

## 启动服务

配置完成后，在项目根目录执行：

```bash
uv run python -m app.server
```
