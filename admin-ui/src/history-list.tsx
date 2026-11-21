import { useEffect, useState } from 'react'
import { BookOpen, ChevronRight, RotateCw, Search, X } from 'lucide-react'
import type { ActivityLayer, HistoryItem, HistoryView } from './types'
import { emptyPage, useHistoryPages, type PageState } from './history-pages'
import { Files } from './history-files'
import { PerceptionBrowser } from './perception-browser'
import { MergedTimeline } from './merged-timeline'
import { OriginalDisclosure } from './compression-browser'
const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'long' })
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeStyle: 'medium',
  hour12: false,
})

function matches(item: HistoryItem, term: string) {
  return [
    item.text,
    item.source,
    item.role,
    item.raw && JSON.stringify(item.raw),
    item.group?.app,
    item.group?.subject,
    ...item.files.map((f) => f.name),
  ].some((field) => field?.toLowerCase().includes(term))
}

function PageActions({
  page,
  load,
  moreLabel,
}: {
  page: PageState
  load: (cursor: string | null) => void
  moreLabel: string
}) {
  return (
    <>
      {page.error && (
        <p className="history-error history-status" role="alert">
          {page.error}
        </p>
      )}
      <div className="history-actions">
        {page.loading ? (
          <p role="status">正在读取记录…</p>
        ) : page.error ? (
          <button onClick={() => load(page.requested)}>重试</button>
        ) : (
          page.next && <button onClick={() => load(page.next)}>{moreLabel}</button>
        )}
      </div>
    </>
  )
}

function groupTime(start: string, end: string) {
  const from = new Date(start),
    to = new Date(end)
  if (!Number.isFinite(from.getTime()) || !Number.isFinite(to.getTime())) return ''
  const first = timeFormat.format(from),
    last = timeFormat.format(to)
  if (dateFormat.format(from) !== dateFormat.format(to))
    return dateFormat.format(from) + ' ' + first + ' – ' + dateFormat.format(to) + ' ' + last
  return first === last ? first : first + ' – ' + last
}

function GroupRecord({
  item,
  pages,
  read,
  term,
}: {
  item: HistoryItem
  term: string
} & ReturnType<typeof useHistoryPages>) {
  const group = item.group!
  const [expanded, setExpanded] = useState(false)
  const page = pages.get(group.id)
  useEffect(() => {
    if (expanded && !page) void read(group.id, null)
  }, [expanded, page, read, group.id])
  const frames = page?.items ?? []
  const visible =
    term && !matches(item, term) ? frames.filter((frame) => matches(frame, term)) : frames
  const title = [...new Set([group.app, group.subject].filter(Boolean))].join(' · ') || '定时截图'
  return (
    <article className="history-entry history-group">
      <button
        className="history-group-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((open) => !open)}
      >
        <ChevronRight size={16} aria-hidden="true" />
        <strong>{title}</strong>
        <span className="history-group-time">{groupTime(group.started_at, group.ended_at)}</span>
      </button>
      <div className="history-group-body">
        {item.text && <p>{item.text}</p>}
        <div className="history-group-meta">
          <span>{group.frame_count} 张截图</span>
          <span>{group.change_count} 次更新</span>
          <span>{item.source}</span>
        </div>
      </div>
      {expanded && (
        <div className="history-group-details">
          <ol className="history-frames" aria-label="截图明细">
            {visible.map((frame) => (
              <li key={frame.id}>
                {frame.occurred_at && (
                  <time dateTime={frame.occurred_at}>
                    {groupTime(frame.occurred_at, frame.occurred_at)}
                  </time>
                )}
                {frame.text && <p>{frame.text}</p>}
                <Files files={frame.files} />
                <OriginalDisclosure id={frame.id} layer="slice" />
              </li>
            ))}
          </ol>
          {page && (
            <PageActions
              page={page}
              load={(cursor) => void read(group.id, cursor)}
              moreLabel="更多截图"
            />
          )}
        </div>
      )}
    </article>
  )
}

function HistoryRecords({
  items,
  pages,
  read,
  term,
  layer,
}: {
  items: HistoryItem[]
  term: string
  layer?: ActivityLayer
} & ReturnType<typeof useHistoryPages>) {
  const rows = []
  let previousDay: string | null = null
  for (const item of items) {
    const stamp = item.occurred_at && new Date(item.occurred_at)
    const date = stamp && Number.isFinite(stamp.getTime()) ? stamp : null
    const day = date ? dateFormat.format(date) : null
    rows.push({ item, date, heading: day !== previousDay ? day : null })
    if (day) previousDay = day
  }
  return (
    <ol className="history-records">
      {rows.map(({ item, date, heading }) => (
        <li key={item.id}>
          {heading && <h2>{heading}</h2>}
          {item.group ? (
            <GroupRecord item={item} pages={pages} read={read} term={term} />
          ) : (
            <article className="history-entry">
              <header>
                <strong>{item.source}</strong>
                <span className="history-role" aria-label={'角色 ' + item.role}>
                  {item.role}
                </span>
                {date ? (
                  <time dateTime={item.occurred_at ?? undefined}>{timeFormat.format(date)}</time>
                ) : (
                  <span className="history-unknown-time">时间未记录</span>
                )}
              </header>
              {item.text && <p>{item.text}</p>}
              <Files files={item.files} />
              {layer && <OriginalDisclosure id={item.id} layer={layer} />}
            </article>
          )}
        </li>
      ))}
    </ol>
  )
}

function ActivityRecords({ items }: { items: HistoryItem[] }) {
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <div className="perception-timeline">
      <div className="trace-heading">
        <h2>活动时间轴</h2>
      </div>
      <ol className="trace-axis" aria-label="活动记录">
        {items.map((item, index) => {
          const at = new Date(item.occurred_at ?? '')
          const day = isNaN(+at) ? '时间未记录' : dateFormat.format(at)
          const prior = new Date(items[index - 1]?.occurred_at ?? '')
          const dayHeading =
            !index || day !== (isNaN(+prior) ? '时间未记录' : dateFormat.format(prior))
          const material = item.material_type
          const kind =
            material === 'slice.audio'
              ? '录音'
              : material === 'slice.image'
                ? '图片'
                : material
                  ? '文本'
                  : item.members
                    ? '合并摘要'
                    : item.layer === 'slice'
                      ? '切片'
                      : '感知'
          const open = selected === item.id
          const preview = item.text.trim() || item.files[0]?.name || kind
          return (
            <li key={item.id}>
              {dayHeading && <h3 className="trace-day">{day}</h3>}
              <button
                className="trace-toggle"
                data-kind="visit"
                aria-expanded={open}
                onClick={() => setSelected(open ? null : item.id)}
              >
                <time dateTime={item.occurred_at ?? undefined}>
                  {isNaN(+at) ? '—' : timeFormat.format(at)}
                </time>
                <span className="trace-point" aria-hidden />
                <span className="trace-title">
                  <strong className="activity-preview">{preview}</strong>
                  <small>
                    {item.source}
                    {item.members && ' · ' + item.members.length + ' 条来源'}
                  </small>
                </span>
                <span className="trace-status">{kind}</span>
                <ChevronRight size={14} className="trace-chevron" aria-hidden />
              </button>
              {open && (
                <div className="trace-detail activity-detail">
                  {item.text && <p>{item.text}</p>}
                  {item.processing && item.processing !== 'ready' && (
                    <small>{item.processing === 'retrying' ? '等待重试' : '等待处理'}</small>
                  )}
                  <Files files={item.files} />
                  {item.members ? (
                    <ol aria-label="摘要来源">
                      {item.members.map((member) => (
                        <li key={member.id}>
                          <span>
                            {groupTime(member.occurred_at, member.occurred_at)} ·{' '}
                            {member.layer === 'slice' ? '切片' : '感知'} · {member.device_id}
                          </span>
                          <OriginalDisclosure id={member.id} layer={member.layer} activity />
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <OriginalDisclosure
                      id={item.id}
                      layer={item.layer === 'perception' ? 'perception' : 'slice'}
                      activity={!material}
                    />
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ol>
    </div>
  )
}

export function HistoryList({ view, label }: { view: HistoryView; label: string }) {
  const { pages, read } = useHistoryPages(view)
  const page = pages.get(null) ?? emptyPage
  const [query, setQuery] = useState('')
  const [revision, setRevision] = useState(0)
  const term = query.trim().toLowerCase()
  const visible = page.items.filter(
    (item) =>
      !term ||
      matches(item, term) ||
      (item.group && pages.get(item.group.id)?.items.some((frame) => matches(frame, term))),
  )
  return (
    <section aria-label={label} className="history-list">
      <div className="history-tools">
        <label className="history-search">
          <Search size={15} aria-hidden="true" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="筛选已加载记录"
            placeholder="筛选已加载记录"
          />
          {query && (
            <button aria-label="清除筛选" onClick={() => setQuery('')}>
              <X size={14} />
            </button>
          )}
        </label>
        <button
          className="history-refresh"
          onClick={() => {
            setRevision((n) => n + 1)
            void read(null, null)
          }}
          disabled={page.loading}
        >
          <RotateCw size={14} aria-hidden="true" /> 刷新
        </button>
      </div>
      {view === 'perception' ? (
        visible.length > 0 && <PerceptionBrowser key={term} items={visible} revision={revision} />
      ) : view === 'timeline' &&
        page.items.some((row) => row.context_write || row.members || row.layer) ? (
        visible.length > 0 && <MergedTimeline items={visible} groupVisits={!term} />
      ) : view === 'slice' &&
        visible.some((row) => row.material_type || row.members || row.layer) ? (
        <ActivityRecords items={visible} />
      ) : (
        <HistoryRecords
          items={visible}
          pages={pages}
          read={read}
          term={term}
          layer={view === 'slice' ? view : undefined}
        />
      )}
      {!page.loading && !page.error && !visible.length && (
        <div className="history-empty" role="status">
          <BookOpen size={22} aria-hidden="true" />
          {term ? '已加载的记录中没有匹配项' : '暂无记录'}
        </div>
      )}
      <PageActions
        page={page}
        load={(cursor) => void read(null, cursor)}
        moreLabel="加载更早记录"
      />
    </section>
  )
}
