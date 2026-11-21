import { originalSource } from './api'
import { useCallback, useState } from 'react'
import { usePages } from './use-pages'
import { ChevronRight } from 'lucide-react'
import type { ActivityLayer, OriginalDetail, OriginalRecord } from './types'
import { Files } from './history-files'

const extractionStates: Record<OriginalDetail['extraction']['status'], string> = {
  ready: '已提取',
  pending: '等待处理',
  retrying: '等待重试',
  no_change: '这次采集没有新增事实',
  not_recorded: '没有已保存的提取结果',
}
const strategies: Record<string, string> = {
  image_description: '截图识别',
  audio_asr_model: '录音转写与摘要',
  rule: '规则提取',
  model: '模型提取',
}
const dates = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' })
const times = new Intl.DateTimeFormat('zh-CN', {
  timeStyle: 'medium',
  hour12: false,
})
function timeRange(start: string, end: string) {
  const from = new Date(start),
    to = new Date(end)
  if (!Number.isFinite(+from) || !Number.isFinite(+to)) return ''
  return (
    dates.format(from) +
    '  ' +
    times.format(from) +
    (start === end
      ? ''
      : ' – ' +
        (dates.format(from) === dates.format(to) ? '' : dates.format(to) + ' ') +
        times.format(to))
  )
}
interface PageControls {
  busy: boolean
  error: string
  next?: string | null
  failedCursor?: string
  read: (cursor?: string) => void
  more: string
}
export function Navigation({ busy, error, next, failedCursor, read, more }: PageControls) {
  if (busy)
    return (
      <p role="status" className="history-status">
        正在读取…
      </p>
    )
  if (error)
    return (
      <div className="history-actions">
        <p className="history-error" role="alert">
          {error}
        </p>
        <button onClick={() => read(failedCursor)}>重试</button>
      </div>
    )
  return next ? (
    <div className="history-actions">
      <button onClick={() => read(next)}>{more}</button>
    </div>
  ) : null
}
type OriginalProps = { id: string; layer: ActivityLayer; activity?: boolean }
export function OriginalData({ id, layer, activity }: OriginalProps) {
  const fetch = useCallback(
    (cursor?: string) => originalSource(id, activity, cursor),
    [id, activity],
  )
  const result = usePages<OriginalRecord, OriginalDetail>(fetch)
  const extraction = result.page?.extraction
  return (
    <div className="compression-source">
      {result.items.map((record) => (
        <section key={record.id} className="compression-source-record">
          <h5>
            原始事件 <span>{record.kind}</span>
          </h5>
          <div className="compression-meta">
            {record.source} · {timeRange(record.occurred_at, record.occurred_at)}
          </div>
          <pre>{JSON.stringify(record.payload, null, 2)}</pre>
          {record.transcript !== null && (
            <>
              <h5>原始转录</h5>
              <p>{record.transcript || '转录为空'}</p>
            </>
          )}
          <Files files={record.files} />
        </section>
      ))}
      {!result.busy && !result.error && !result.items.length && <p>暂无原始资料</p>}
      {extraction && (
        <section>
          <h5>{layer === 'slice' ? '切片提取结果' : '感知活动文字'}</h5>
          {(extraction.strategy || extraction.source_count > 1) && (
            <div className="compression-meta">
              {[
                extraction.strategy && (strategies[extraction.strategy] ?? extraction.strategy),
                extraction.source_count > 1 && `合并 ${extraction.source_count} 条原始记录`,
              ]
                .filter(Boolean)
                .join(' · ')}
            </div>
          )}
          {extraction.text && <p>{extraction.text}</p>}
          {(!extraction.text || extraction.status !== 'ready') && (
            <small>{extractionStates[extraction.status]}</small>
          )}
        </section>
      )}
      <Navigation {...result} more="加载更多原始资料" />
    </div>
  )
}

export function OriginalDisclosure(props: OriginalProps) {
  const [open, setOpen] = useState(false)
  return (
    <div className="original-disclosure">
      <button className="history-inline" aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronRight size={13} aria-hidden />
        {open ? '收起原始资料' : '查看原始资料'}
      </button>
      {open && <OriginalData {...props} />}
    </div>
  )
}
