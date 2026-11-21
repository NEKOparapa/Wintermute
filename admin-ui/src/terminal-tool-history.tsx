import { connectorHistory } from './api'
import { useEffect, useState } from 'react'
import { Plug, RotateCw } from 'lucide-react'
import type { ConnectorCall, ResultsPage } from './types'

const statuses: Record<ConnectorCall['status'], string> = {
  queued: '等待设备领取',
  running: '执行中',
  succeeded: '已成功',
  failed: '执行失败',
  timed_out: '执行超时',
  expired: '已过期',
}

const toolTypeLabels: Record<ConnectorCall['tool_type'], string> = {
  mcp: 'MCP',
  device: '设备',
  cli: 'CLI',
}

type TypeFilter = 'all' | 'mcp' | 'device'

function Time({ at }: { at: string }) {
  return (
    <time dateTime={at}>
      {new Date(at).toLocaleString('zh-CN', {
        dateStyle: 'short',
        timeStyle: 'medium',
        hour12: false,
      })}
    </time>
  )
}

function CallRecord({ call }: { call: ConnectorCall }) {
  return (
    <article className="history-entry terminal-call">
      <header>
        <strong>{call.connector_name || call.connector_id}</strong>
        <code>{toolTypeLabels[call.tool_type]}</code>
        <code>{call.tool}</code>
        <span className="call-status" data-status={call.status}>
          {statuses[call.status]}
        </span>
        <Time at={call.created_at} />
      </header>
      {typeof call.arguments.command === 'string' ? (
        <pre aria-label="命令">{call.arguments.command}</pre>
      ) : (
        Object.keys(call.arguments).length > 0 && (
          <pre aria-label="工具参数">{JSON.stringify(call.arguments, null, 2)}</pre>
        )
      )}
      <details>
        <summary>执行详情</summary>
        <dl>
          <div>
            <dt>连接器 ID</dt>
            <dd>{call.connector_id}</dd>
          </div>
          <div>
            <dt>{call.tool_type === 'mcp' ? '账号 / 连接' : '目标设备'}</dt>
            <dd>{call.target}</dd>
          </div>
          <div>
            <dt>发起时间</dt>
            <dd>
              <Time at={call.created_at} />
            </dd>
          </div>
          {call.started_at && (
            <div>
              <dt>开始时间</dt>
              <dd>
                <Time at={call.started_at} />
              </dd>
            </div>
          )}
          {call.completed_at && (
            <div>
              <dt>结束时间</dt>
              <dd>
                <Time at={call.completed_at} />
              </dd>
            </div>
          )}
          {call.timeout_seconds !== null && (
            <div>
              <dt>超时上限</dt>
              <dd>{call.timeout_seconds} 秒</dd>
            </div>
          )}
          {call.exit_code !== null && (
            <div>
              <dt>退出码</dt>
              <dd>{call.exit_code}</dd>
            </div>
          )}
        </dl>
        {call.output && (
          <section>
            <h3>{call.tool_type === 'mcp' ? '返回结果' : '标准输出'}</h3>
            <pre>{call.output}</pre>
          </section>
        )}
        {call.error && (
          <section>
            <h3>{call.tool_type === 'mcp' ? '错误信息' : '标准错误'}</h3>
            <pre>{call.error}</pre>
          </section>
        )}
        {call.completed_at && !call.output && !call.error && <small>没有输出</small>}
        {call.truncated && <small>输出已截断</small>}
      </details>
    </article>
  )
}

function CallPage({
  cursor,
  older,
  newer,
}: {
  cursor?: string
  older: (cursor: string) => void
  newer: () => void
}) {
  const [page, setPage] = useState<ResultsPage<ConnectorCall> | null>(null)
  const [filter, setFilter] = useState<TypeFilter>('all')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    let active = true
    let pending = false
    async function read(interactive = false) {
      if (pending) return
      pending = true
      if (interactive) setLoading(true)
      try {
        const result = await connectorHistory(cursor)
        if (active) {
          setPage(result)
          setError('')
        }
      } catch (cause) {
        if (active)
          setError(
            cause instanceof Error
              ? cause.message.replace(/^.*?Error: /, '')
              : '读取连接器调用记录失败',
          )
      } finally {
        pending = false
        if (active) setLoading(false)
      }
    }
    function refreshVisible() {
      if (document.visibilityState === 'visible') void read()
    }
    void read(true)
    const timer = window.setInterval(refreshVisible, 5000)
    document.addEventListener('visibilitychange', refreshVisible)
    window.addEventListener('focus', refreshVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshVisible)
      window.removeEventListener('focus', refreshVisible)
    }
  }, [cursor, revision])

  const visibleItems = (page?.items ?? []).filter((item) => {
    if (filter === 'all') return true
    return item.tool_type === filter
  })

  return (
    <section aria-label="连接器调用记录">
      <div className="history-tools terminal-call-tools">
        <div role="group" aria-label="按连接器类型筛选" className="connector-filters">
          {(
            [
              ['all', '全部'],
              ['device', '桌面 / 设备'],
              ['mcp', '远程 MCP'],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className="history-refresh"
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          className="history-refresh"
          onClick={() => setRevision((n) => n + 1)}
          disabled={loading}
        >
          <RotateCw size={14} aria-hidden="true" /> 刷新
        </button>
      </div>
      {error && (
        <p className="history-error history-status" role="alert">
          {error}
        </p>
      )}
      {loading && !page && (
        <p className="history-status" role="status">
          正在读取连接器调用记录…
        </p>
      )}
      {page && !visibleItems.length && !error && (
        <div className="history-empty" role="status">
          <Plug size={22} aria-hidden="true" />
          暂无连接器调用记录
        </div>
      )}
      <ol className="history-records">
        {visibleItems.map((call) => (
          <li key={call.id}>
            <CallRecord call={call} />
          </li>
        ))}
      </ol>
      <div className="history-actions terminal-call-pages">
        {cursor && <button onClick={newer}>较新记录</button>}
        {page?.next_cursor && <button onClick={() => older(page.next_cursor!)}>较早记录</button>}
      </div>
    </section>
  )
}

export function TerminalToolHistory() {
  const [positions, setPositions] = useState<string[]>([])
  const cursor = positions.at(-1)
  return (
    <CallPage
      key={cursor ?? ''}
      cursor={cursor}
      older={(next) => setPositions((previous) => [...previous, next])}
      newer={() => setPositions((previous) => previous.slice(0, -1))}
    />
  )
}
