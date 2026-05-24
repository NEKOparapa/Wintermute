# Wintermute
个人AI结合体

## 启动服务

```powershell
uv run python -m app.server
```

## HTTP 接口

健康检查：

```powershell
Invoke-RestMethod -Method Get http://127.0.0.1:8000/health
```

发消息：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/event `
  -ContentType "application/json" `
  -Body '{"message":"你好"}'
```

手动触发某层级压缩（方便不等定时任务时调试和回填）：

```powershell
# daily：压缩指定一天的全部原始事件
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/memory/rollup `
  -ContentType "application/json" `
  -Body '{"kind":"daily","date":"2026-05-23"}'

# weekly：压缩指定 ISO 周的所有 daily 记忆
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/memory/rollup `
  -ContentType "application/json" `
  -Body '{"kind":"weekly","year":2026,"week":21}'

# monthly：压缩指定月的所有 weekly 记忆
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/memory/rollup `
  -ContentType "application/json" `
  -Body '{"kind":"monthly","year":2026,"month":5}'
```

## 数据存放

- `data/events/YYYY-MM-DD.json` — 原始事件，按日期分文件，append-only。
- `data/memories/session/YYYY-MM-DD.json` — 会话内压缩的记忆。
- `data/memories/daily/YYYY-MM-DD.json` — 每日压缩的记忆。
- `data/memories/weekly/YYYY-Www.json` — 每周压缩，文件名用 ISO 周编号。
- `data/memories/monthly/YYYY-MM.json` — 每月压缩。

各层之间通过 `source_event_ids` / `source_memory_ids` 建立溯源链，可查可追溯。

## 配置

`config/settings.json` 覆盖默认值即可，所有字段都是可选的：

```json
{
  "api_key": "sk-...",
  "model": "gpt-4o",
  "base_url": "https://api.openai.com/v1",

  "recent_rounds": 5,
  "session_compress_trigger_tokens": 4000,

  "prompt_budget_session_tokens": 8000,
  "prompt_budget_daily_tokens": 8000,
  "prompt_budget_weekly_tokens": 4000,
  "prompt_budget_monthly_tokens": 2000,

  "daily_rollup_hour": 3,
  "daily_rollup_minute": 0,

  "weekly_rollup_weekday": 0,
  "weekly_rollup_hour": 3,
  "weekly_rollup_minute": 30,

  "monthly_rollup_day": 1,
  "monthly_rollup_hour": 4,
  "monthly_rollup_minute": 0
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `recent_rounds` | prompt 中保留最近多少轮原始对话；之前的部分由记忆负责 |
| `session_compress_trigger_tokens` | 当"最近 N 轮之前的未压缩事件"超过这个 token 数，触发会话内压缩 |
| `prompt_budget_*_tokens` | 每层级注入到 prompt 的 token 预算上限 |
| `daily_rollup_*` | 每天压缩的执行时刻（系统本地时间） |
| `weekly_rollup_*` | 每周压缩。`weekday` 用 Python 约定：周一=0，周日=6 |
| `monthly_rollup_*` | 每月压缩。`day` 是月内第几天 |

启动时会跑一次 `catch_up_recent`，自动补上最近错过的 daily/weekly/monthly 压缩。
