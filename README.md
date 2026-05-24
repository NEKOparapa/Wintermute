# Wintermute
个人AI结合体

## 启动服务

```powershell
uv run python -m app.server
```

## HTTP 接口

```powershell
Invoke-RestMethod -Method Get http://127.0.0.1:8000/health
```

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/event `
  -ContentType "application/json" `
  -Body '{"message":"你好"}'
```

对话历史会自动按日期写入 `data/events/YYYY-MM-DD.json`，分层记忆写入 `data/memories/`。
