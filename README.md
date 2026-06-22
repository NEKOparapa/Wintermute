# Wintermute

个人AI结合体

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
