import { useCallback, useEffect, useRef, useState } from 'react'
import { BookOpen, ChevronRight, RotateCw, Search } from 'lucide-react'
import type { ImportedRecord, RecordContent, RecordType, ResultsPage } from './types'
import { usePages } from './use-pages'
import { recordContent, recordItems } from './api'

const types: Record<RecordType, string> = {
  sms: '短信',
  call_log: '通话',
  contacts: '通讯录',
  notion: 'Notion',
  recording_list: '录音列表',
  document: '文档',
}
const states: Record<ImportedRecord['status'], string> = {
  pending: '待整理',
  processing: '整理中',
  processed: '已整理',
  filtered: '已过滤',
  failed: '整理失败',
}
const dayFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'long' })
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function RecordOriginal({ id }: { id: string }) {
  const [content, setContent] = useState<RecordContent | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)
  const pending = useRef<symbol | null>(null)
  const read = useCallback(
    async (offset = 0) => {
      if (pending.current) return
      const token = Symbol()
      pending.current = token
      setBusy(true)
      setError('')
      try {
        const page = await recordContent(id, offset)
        if (pending.current === token)
          setContent((previous) => ({
            ...page,
            text: (offset ? (previous?.text ?? '') : '') + page.text,
          }))
      } catch (e) {
        if (pending.current === token) setError(e instanceof Error ? e.message : '读取原文失败。')
      } finally {
        if (pending.current === token) {
          pending.current = null
          setBusy(false)
        }
      }
    },
    [id],
  )
  useEffect(() => {
    let live = true
    queueMicrotask(() => {
      if (live) void read()
    })
    return () => {
      live = false
      pending.current = null
    }
  }, [read])
  return (
    <div className="record-original">
      <h3>记录原文</h3>
      {content && <pre aria-label="记录原文">{content.text || '（空内容）'}</pre>}
      {error && (
        <p className="history-error" role="alert">
          {error}
        </p>
      )}
      <div className="record-content-actions">
        {content && (
          <span>
            {(content.next_offset ?? content.total_chars).toLocaleString()} /{' '}
            {content.total_chars.toLocaleString()} 字符
          </span>
        )}
        {busy ? (
          <span role="status">正在读取原文…</span>
        ) : error ? (
          <button onClick={() => void read(content?.next_offset ?? 0)}>重试读取</button>
        ) : (
          content?.next_offset != null && (
            <button onClick={() => void read(content.next_offset!)}>继续加载原文</button>
          )
        )}
      </div>
    </div>
  )
}

function RecordRow({ item }: { item: ImportedRecord }) {
  const [open, setOpen] = useState(false)
  const status = item.status === 'pending' && item.attempts > 0 ? '等待重试' : states[item.status]
  return (
    <>
      <button className="merged-row" aria-expanded={open} onClick={() => setOpen(!open)}>
        <time
          dateTime={item.occurred_at}
          title={new Date(item.occurred_at).toLocaleString('zh-CN')}
        >
          {timeFormat.format(new Date(item.occurred_at))}
        </time>
        <span className="merged-mark" aria-hidden="true">
          <i />
        </span>
        <span className="merged-label">
          <strong>{item.title || types[item.record_type]}</strong>
          <span>
            {types[item.record_type]} · {item.source_name}
          </span>
        </span>
        <span className="record-state" data-status={item.status}>
          {status}
        </span>
        <ChevronRight className="merged-chevron" size={14} aria-hidden="true" />
      </button>
      {open && (
        <div className="merged-details record-details">
          <dl>
            <div>
              <dt>发生时间</dt>
              <dd>{new Date(item.occurred_at).toLocaleString('zh-CN')}</dd>
            </div>
            <div>
              <dt>导入时间</dt>
              <dd>{new Date(item.received_at).toLocaleString('zh-CN')}</dd>
            </div>
            <div>
              <dt>来源</dt>
              <dd>{item.source_name}</dd>
            </div>
            {item.external_id && (
              <div>
                <dt>来源编号</dt>
                <dd>{item.external_id}</dd>
              </div>
            )}
            <div>
              <dt>导入批次</dt>
              <dd>{item.batch_id}</dd>
            </div>
          </dl>
          {item.error && (
            <p className={item.status === 'failed' ? 'history-error' : 'record-hint'}>
              {item.error === 'record_processing_failed'
                ? `整理未完成，已尝试 ${item.attempts} 次。`
                : item.error}
            </p>
          )}
          <RecordOriginal id={item.id} />
        </div>
      )}
    </>
  )
}

function RecordList({ recordType }: { recordType?: RecordType }) {
  const fetch = useCallback(
    (cursor?: string) => {
      return recordItems(cursor, recordType)
    },
    [recordType],
  )
  const { items, busy, error, next, failedCursor, read } = usePages<
    ImportedRecord,
    ResultsPage<ImportedRecord>
  >(fetch)
  const [query, setQuery] = useState('')
  const term = query.trim().toLocaleLowerCase()
  const visible = items.filter(
    (item) =>
      !term ||
      [item.title, item.source_name, item.external_id, types[item.record_type]].some((value) =>
        value?.toLocaleLowerCase().includes(term),
      ),
  )
  return (
    <>
      <div className="history-tools">
        <label className="history-search">
          <Search size={15} aria-hidden="true" />
          <input
            type="search"
            aria-label="筛选已加载记录"
            placeholder="筛选已加载记录"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button className="history-refresh" disabled={busy} onClick={() => read()}>
          <RotateCw size={14} aria-hidden="true" /> 刷新
        </button>
      </div>
      <div className="merged-timeline record-timeline">
        <ol aria-label="记录时间线">
          {visible.map((item, index) => {
            const day = dayFormat.format(new Date(item.occurred_at))
            const heading =
              index === 0 || day !== dayFormat.format(new Date(visible[index - 1].occurred_at))
            return (
              <li key={item.id} data-day-start={heading || undefined}>
                {heading && <h2 className="merged-day">{day}</h2>}
                <RecordRow item={item} />
              </li>
            )
          })}
        </ol>
      </div>
      {!busy && !error && !visible.length && (
        <div className="history-empty" role="status">
          <BookOpen size={22} aria-hidden="true" />
          <p>{term ? '已加载的记录中没有匹配项' : '暂无记录'}</p>
          {!term && (
            <p className="record-hint">导入的短信、通话、通讯录、笔记和文档会显示在这里。</p>
          )}
        </div>
      )}
      {error && (
        <p className="history-error history-status" role="alert">
          {error}
        </p>
      )}
      <div className="history-actions">
        {busy ? (
          <p role="status">正在读取记录…</p>
        ) : error ? (
          <button onClick={() => read(failedCursor)}>重试</button>
        ) : (
          next && <button onClick={() => read(next)}>加载更早记录</button>
        )}
      </div>
    </>
  )
}

export function RecordTimeline() {
  const [recordType, setRecordType] = useState<RecordType | ''>('')
  return (
    <section aria-label="记录层">
      <div className="record-heading">
        <p className="record-hint">按原始发生时间倒序排列，展开查看原文。</p>
        <label>
          记录类型
          <select
            aria-label="记录类型"
            value={recordType}
            onChange={(e) => setRecordType(e.target.value as RecordType | '')}
          >
            <option value="">全部类型</option>
            {Object.entries(types).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <RecordList key={recordType} recordType={recordType || undefined} />
    </section>
  )
}
