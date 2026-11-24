import { perceptionStages } from './api'
import { useEffect, useRef, useState } from 'react'
import { ChevronRight, NotebookText } from 'lucide-react'
import type {
  HistoryItem,
  PerceptionStages,
  SummaryRoundRef,
  TimelineUsage,
  TraceMember,
} from './types'
import { duration, formatVisitText, perceptionLabel, record, text } from './perception'
import { PerceptionRecord } from './perception-record'
import { PerceptionLinks, type TraceNavigation } from './perception-links'

const date = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' })
const clock = new Intl.DateTimeFormat('zh-CN', {
  timeStyle: 'medium',
  hour12: false,
})

const outcomes = {
  filtered: '整条过滤',
  partial: '部分内容已过滤',
  kept: '完整保留',
  unprojected: '未形成活动',
} as const

const inputOutcomes = {
  kept: '保留',
  normalized: '已整理空白字符',
  redacted: '已脱敏',
  empty: '空文本，已过滤',
  too_long: '超过 500 字，已过滤',
  invalid_time: '时间无效，已过滤',
}

function RuleResult({ data }: { data: PerceptionStages }) {
  if (data.filter_reason === 'wintermute_app') return <p>Wintermute 自身活动固定整条过滤。</p>
  if (data.filter_reason === 'location_nearby') return <p>与上一有效位置相距不足 300 米，已过滤。</p>
  if (data.filter_reason === 'location_stale') return <p>采样早于上一有效位置，已过滤。</p>
  if (data.filter_reason === 'location_unresolved') return <p>Photon 未找到对应地点。</p>
  if (data.filter_reason === 'mobile_home') return <p>已过滤手机桌面启动器的停留记录。</p>
  if (data.filter_reason === 'mobile_zero_duration') return <p>手机上报的前台停留时长不足 1 秒，已整条过滤。</p>
  if (!data.parts.length && data.filter_reason !== 'short_dwell')
    return <p className="stage-note">这次上报没有在本会话形成新的活动记录。</p>
  // Servers predating per-record thresholds used three seconds.
  const threshold = data.dwell_threshold_seconds ?? 3
  const titles = data.decisions.flatMap((row) => (row.kind === 'title' ? [row] : []))
  const inputs = data.decisions.filter((row) => row.kind === 'input')
  return (
    <>
      {data.retained === null && <p>整次停留已过滤</p>}
      {titles.length > 0 && (
        <table className="rule-decisions">
          <thead>
            <tr>
              <th>工作对象</th>
              <th>停留</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            {titles.map((row, index) => (
              <tr key={index} data-kept={row.kept}>
                <td>{row.label}</td>
                <td>
                  {Math.trunc(row.seconds).toLocaleString('zh-CN')} 秒
                </td>
                <td>
                  {row.seconds > threshold
                    ? `保留（超过 ${threshold} 秒）`
                    : row.kept
                      ? '保留（有文本输入）'
                      : `过滤（${threshold} 秒及以内）`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {inputs.length > 0 && (
        <ul aria-label="输入文本过滤结果">
          {inputs.map((row) => (
            <li key={row.label}>{row.label} · {inputOutcomes[row.action]}</li>
          ))}
        </ul>
      )}
      {data.retained !== null && (
        <details className="stage-disclosure">
          <summary>查看过滤后保留的字段</summary>
          <pre className="perception-json">{JSON.stringify(data.retained, null, 2)}</pre>
        </details>
      )}
    </>
  )
}

function Formatted({ data }: { data: PerceptionStages }) {
  const parts = data.parts.filter((part) => part.text)
  if (!parts.length)
    return (
      <p className="stage-note">
        {data.parts.length || data.filter_reason ? '整条记录已过滤。' : '暂无精简文本。'}
      </p>
    )
  return (
    <>
      {parts.map((part) => (
        <div key={part.kind} className="stage-section">
          <div className="stage-caption">
            <span>{part.kind === 'visual' ? '截图识别补充' : '精简文本'}</span>
            {part.tokens != null && <small>{part.tokens.toLocaleString('zh-CN')} tokens</small>}
          </div>
          <pre className="stage-text">
            {part.kind === 'observation' ? formatVisitText(part.text) : part.text}
          </pre>
        </div>
      ))}
    </>
  )
}

function VisitDetails({
  id,
  item,
  revision,
  navigation,
}: {
  id: string
  item?: HistoryItem
  revision: number
  navigation: TraceNavigation
}) {
  const [data, setData] = useState<PerceptionStages>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const needsRecord = !item
  useEffect(() => {
    let mounted = true
    let reading = false
    async function read() {
      if (reading || document.visibilityState === 'hidden') return
      reading = true
      try {
        const next = await perceptionStages(id, needsRecord)
        if (mounted) {
          setData(next)
          setError('')
        }
      } catch (e) {
        if (mounted)
          setError(e instanceof Error ? e.message.replace(/^.*?Error: /, '') : '读取失败。')
      } finally {
        reading = false
      }
    }
    void read()
    const timer = window.setInterval(read, 5000)
    window.addEventListener('focus', read)
    return () => {
      mounted = false
      clearInterval(timer)
      window.removeEventListener('focus', read)
    }
  }, [id, needsRecord, revision, retry])
  const raw = item ?? data?.record
  return (
    <div className="perception-stage">
      {raw && (
        <section>
          <h3>原始信息</h3>
          <PerceptionRecord item={raw} />
        </section>
      )}
      {error && (
        <div role="alert" className="history-error">
          {error} <button onClick={() => setRetry((n) => n + 1)}>重试</button>
        </div>
      )}
      {!data && !error && <p role="status">正在读取处理记录…</p>}
      {data && (
        <>
          <section>
            <h3>过滤处理</h3>
            <RuleResult data={data} />
          </section>
          <section>
            <h3>压缩处理</h3>
            <Formatted data={data} />
            <div className="trace-shortcuts">
              {data.rounds.map(
                ({ timeline_entry: ref }) =>
                  ref && (
                    <button
                      key={ref.id}
                      className="history-inline"
                      onClick={() => navigation.openSummary(ref)}
                    >
                      查看关联摘要
                      {data.rounds.length > 1 && ' · ' + clock.format(new Date(ref.occurred_at))} ↗
                    </button>
                  ),
              )}
            </div>
          </section>
        </>
      )}
    </div>
  )
}

type TimelineRow = {
  id: string
  at: string | null
  title: string
  usage?: TimelineUsage
  device?: string
  duration?: string
  timeNote?: string
  status: string
  kind: 'visit' | 'summary'
  item?: HistoryItem
}

function visitRow(item: HistoryItem): TimelineRow {
  const info = perceptionLabel(item)
  const value = record(record(item.raw?.payload).value)
  return {
    kind: 'visit',
    id: item.id,
    at: text(value.started_at) || item.occurred_at,
    title: info.title,
    usage: item.usage,
    device: [text(value.device_platform), text(value.device_name) || text(item.raw?.device_id)]
      .filter(Boolean)
      .join(' '),
    duration: info.duration,
    status: item.filter_result ? outcomes[item.filter_result] : '',
    item,
  }
}

function summaryRow(ref: SummaryRoundRef): TimelineRow {
  return {
    kind: 'summary',
    id: ref.id,
    at: ref.occurred_at,
    title: 'AI 摘要',
    status: '已生成',
    usage: ref.usage,
    timeNote: ref.generated_at ? undefined : '生成时刻未记录，按本轮开始时间排列',
  }
}

const atSameTime = { visit: 0, summary: 1 }

function timelineTime(row: TimelineRow) {
  const time = Date.parse(row.at ?? '')
  return Number.isFinite(time) ? time : -Infinity
}

export function PerceptionBrowser({ items, revision }: { items: HistoryItem[]; revision: number }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [linked, setLinked] = useState<TimelineRow[]>([])
  const [outcome, setOutcome] = useState('all')
  const focusTarget = useRef<string | null>(null)
  const rows = new Map<string, TimelineRow>()
  for (const row of linked) rows.set(row.id, row)
  for (const item of items) {
    if (outcome !== 'all' && item.filter_result !== outcome) continue
    rows.set(item.id, item.summary_round ? summaryRow(item.summary_round) : visitRow(item))
  }
  const ordered = [...rows.values()].sort(
    (a, b) => timelineTime(b) - timelineTime(a) || atSameTime[b.kind] - atSameTime[a.kind],
  )
  function jump(row: TimelineRow) {
    setOutcome('all')
    if (!rows.has(row.id)) setLinked((prior) => [...prior, row])
    setSelected(row.id)
    focusTarget.current = row.id
  }
  const navigation: TraceNavigation = {
    openMember: (member: TraceMember) => {
      if (member.event_id)
        jump({
          kind: 'visit',
          id: member.event_id,
          at: member.occurred_at,
          title: member.title,
          device: member.device_id,
          usage: member.usage,
          status: '关联记录',
        })
    },
    openSummary: (ref) => jump(summaryRow(ref)),
  }
  useEffect(() => {
    if (!selected || focusTarget.current !== selected) return
    const button = document.getElementById('perception-row-' + selected)
    button?.focus({ preventScroll: true })
    button?.scrollIntoView({ block: 'start' })
    focusTarget.current = null
  }, [selected])
  return (
    <div className="perception-timeline">
      <div className="trace-heading">
        <h2>活动时间轴</h2>
        {items.some((i) => i.filter_result) && (
          <label className="perception-outcome">
            过滤结果
            <select
              aria-label="按过滤结果筛选"
              value={outcome}
              onChange={(e) => {
                setOutcome(e.target.value)
                setLinked([])
                setSelected(null)
              }}
            >
              <option value="all">全部记录</option>
              {Object.entries(outcomes).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <ol className="trace-axis" aria-label="采集记录">
        {ordered.map((row, i) => {
          const at = row.at ? new Date(row.at) : null
          const stamp = at && Number.isFinite(+at) ? at : null
          const day = stamp ? date.format(stamp) : '时间未记录'
          const previous = i && ordered[i - 1].at ? new Date(ordered[i - 1].at!) : null
          const open = selected === row.id
          const usage = row.usage
          const devices = usage?.devices.join('、') || row.device || '未记录'
          const elapsed =
            usage?.duration_seconds != null
              ? duration(usage.duration_seconds)
              : row.duration || '未记录'
          const previousDay =
            previous && Number.isFinite(+previous) ? date.format(previous) : '时间未记录'
          return (
            <li key={row.id}>
              {(i === 0 || day !== previousDay) && <h3 className="trace-day">{day}</h3>}
              <button
                id={'perception-row-' + row.id}
                className="trace-toggle"
                data-kind={row.kind}
                aria-expanded={open}
                aria-controls={open ? 'perception-detail-' + row.id : undefined}
                onClick={() => {
                  focusTarget.current = open ? null : row.id
                  setSelected(open ? null : row.id)
                }}
              >
                <time dateTime={row.at ?? undefined} title={row.timeNote}>
                  {stamp ? clock.format(stamp) : '—'}
                </time>
                <span className="trace-point" aria-hidden>
                  {row.kind === 'summary' && <NotebookText size={14} />}
                </span>
                <span className="trace-title">
                  <span className="trace-title-line">
                    <strong>{row.title}</strong>
                    <span
                      className="trace-budget"
                      title={usage?.tokenizer ? 'DeepSeek 分词器统计的文本用量' : undefined}
                    >
                      {usage
                        ? `${usage.tokenizer ? '' : '约 '}${usage.tokens.toLocaleString('zh-CN')} tokens`
                        : 'token 数未记录'}
                    </span>
                  </span>
                  <small title={`来源设备 ${devices} · 使用时长 ${elapsed}`}>
                    来源设备 {devices} · 使用时长 {elapsed}
                  </small>
                </span>
                <span className="trace-status">{row.status}</span>
                <ChevronRight size={14} className="trace-chevron" aria-hidden />
              </button>
              {open && (
                <div className="trace-detail" id={'perception-detail-' + row.id}>
                  {row.kind !== 'visit' ? (
                    <PerceptionLinks target={row.id} {...navigation} />
                  ) : (
                    <VisitDetails
                      id={row.id}
                      item={row.item}
                      revision={revision}
                      navigation={navigation}
                    />
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ol>
      {!ordered.length && (
        <p className="stage-note" role="status">
          已加载的记录中没有符合此筛选条件的停留。
        </p>
      )}
    </div>
  )
}
