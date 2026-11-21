import { useState } from 'react'
import { ChevronRight, Layers, MessageSquareText, StickyNote } from 'lucide-react'
import type { HistoryItem } from './types'
import { duration } from './perception'

const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'long' })
const clock = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})
const preciseClock = new Intl.DateTimeFormat('zh-CN', { timeStyle: 'medium', hour12: false })

// The server also delivers this text to the Agent. Strip only its known envelope for display.
const envelope = /^\d{4}-\d\d-\d\dT[^|\n]+? \| (感知|切片)( · [^\n|]+)? \| /
const windowSpan =
  /^(.+?) \d{2}:\d{2}:\d{2}–\d{2}:\d{2}:\d{2} (\d+(?:\.\d+)?) 秒\r?\n\s*内容：[ \t]*([^\n]*)/
const entryClock = /^(?:\d\d-\d\d )?\d\d:\d\d:\d\d\s+/

function displayText(item: HistoryItem) {
  const text = item.text.trim()
  if (item.context_write)
    return { title: '写入上下文', description: '', seconds: null, origin: item.source }
  if (item.members) {
    // A provisional summary may contain token-limited fragments of generated event lines.
    const layers = new Set(
      item.members.map((member) => (member.layer === 'slice' ? '切片' : '感知')),
    )
    const description = /^\d{4}-\d\d-\d\dT/.test(text) ? [...layers].join('、') + '活动' : text
    const kind = item.merged_summary?.kind
    const title = kind === 'ai' ? 'AI 摘要' : kind === 'rules' ? '规则摘要' : '合并摘要'
    return { title, description, seconds: null, origin: item.source }
  }
  const header = text.match(envelope)
  const body = header ? text.slice(header[0].length).trim() : text
  const span = item.layer === 'perception' && body.match(windowSpan)
  if (span) {
    const app = span[1].trim()
    const content = span[3].split(/(?:^|；)画面记录：/)[0].replace(/^工作对象：/, '')
    const titles = content
      .split(' → ')
      .map((title) => title.replace(entryClock, '').trim())
      .filter(
        (title) =>
          title &&
          title.toLowerCase() !== app.toLowerCase() &&
          !/^中间略去 \d+ 次切换$/.test(title),
      )
    return {
      title: app.toLowerCase() === 'chrome' ? 'Chrome' : app,
      description: [...new Set(titles)].join(' → '),
      seconds: Number(span[2]),
      origin: header?.[2] ?? item.source,
    }
  }
  return {
    title:
      item.layer === 'slice'
        ? header?.[2]?.endsWith(' · 录音')
          ? '录音片段'
          : '切片'
        : '设备活动',
    description: body || item.files[0]?.name || '',
    seconds: null,
    origin: item.source,
  }
}

function entry(item: HistoryItem) {
  const date = new Date(item.occurred_at ?? '')
  const valid = Number.isFinite(date.getTime())
  return {
    ...displayText(item),
    item,
    date,
    day: valid ? dateFormat.format(date) : '日期未记录',
    time: valid ? clock.format(date) : '—',
    preciseTime: valid ? preciseClock.format(date) : '时间未记录',
  }
}

type Entry = ReturnType<typeof entry>

function visitGroups(items: HistoryItem[], groupVisits: boolean) {
  const groups: Entry[][] = []
  for (const item of items) {
    const row = entry(item)
    const group = groups.at(-1)
    const prev = group?.at(-1)
    const gap = prev ? +prev.date - +row.date : NaN
    if (
      groupVisits &&
      group &&
      prev &&
      row.seconds !== null &&
      prev.seconds !== null &&
      row.title === prev.title &&
      row.origin === prev.origin &&
      row.item.source === prev.item.source &&
      row.day === prev.day &&
      gap >= 0 &&
      gap <= 120_000
    ) {
      group.push(row)
    } else {
      groups.push([row])
    }
  }
  return groups
}

function elapsed(seconds: number) {
  return seconds > 0 && seconds < 1 ? '不足 1 秒' : duration(Math.round(seconds))
}

function coverageLabel({ item, day }: Entry) {
  const info = item.context_write ?? item.merged_summary
  if (!info?.covered_from || !info.covered_until) return ''
  const from = new Date(info.covered_from)
  const to = new Date(info.covered_until)
  if (!Number.isFinite(+from) || !Number.isFinite(+to)) return ''
  const startDay = dateFormat.format(from),
    endDay = dateFormat.format(to)
  const start =
    (startDay === day && endDay === day ? '' : startDay + ' ') + preciseClock.format(from)
  const end = (endDay === startDay ? '' : endDay + ' ') + preciseClock.format(to)
  return '覆盖 ' + (from.getTime() === to.getTime() ? start : start + ' – ' + end)
}

export function MergedTimeline({
  items,
  groupVisits,
}: {
  items: HistoryItem[]
  groupVisits: boolean
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const groups = visitGroups(items, groupVisits)
  return (
    <section className="merged-timeline" aria-label="活动时间轴">
      <ol aria-label="活动记录">
        {groups.map((group, index) => {
          const [first] = group
          const open = group.some((row) => row.item.id === selected)
          const summary = !!first.item.members
          const info = first.item.merged_summary
          const write = first.item.context_write
          const missingGeneration = info?.kind === 'ai' && !info.generated_at
          const coverage = write || summary ? coverageLabel(first) : ''
          const multiple = group.length > 1
          const description = [
            ...new Set(group.map((row) => row.description).filter(Boolean)),
          ].join(' · ')
          const metadata =
            first.seconds !== null
              ? (multiple ? group.length + ' 次 · 共 ' : '') +
                elapsed(group.reduce((sum, row) => sum + (row.seconds ?? 0), 0))
              : first.item.members
                ? first.item.members.length + ' 条来源'
                : ''
          return (
            <li key={first.item.id}>
              {first.day !== groups[index - 1]?.[0].day && (
                <h2 className="merged-day">{first.day}</h2>
              )}
              <button
                type="button"
                className="merged-row"
                aria-expanded={open}
                onClick={() => setSelected(open ? null : first.item.id)}
              >
                {missingGeneration ? (
                  <span className="merged-unknown-time" title="生成时间未记录">
                    —
                  </span>
                ) : (
                  <time
                    dateTime={first.item.occurred_at ?? undefined}
                    title={
                      write
                        ? '写入时间 · ' + first.preciseTime
                        : info?.generated_at
                          ? '生成于 ' + first.preciseTime
                          : first.preciseTime
                    }
                  >
                    {first.time}
                  </time>
                )}
                <span className="merged-mark" aria-hidden="true">
                  {write ? (
                    <MessageSquareText size={15} />
                  ) : summary ? (
                    <Layers size={15} />
                  ) : first.item.layer === 'slice' ? (
                    <StickyNote size={14} />
                  ) : (
                    <i />
                  )}
                </span>
                <span className="merged-label">
                  <strong>{first.title}</strong>
                  {coverage && !(write && open) && (
                    <span className="merged-coverage">
                      {coverage}
                      {missingGeneration && ' · 生成时间未记录'}
                    </span>
                  )}
                  {description && <span>{description}</span>}
                </span>
                <span className="merged-amount">{metadata}</span>
                <ChevronRight className="merged-chevron" size={14} aria-hidden="true" />
              </button>
              {open && (
                <div
                  className="merged-details"
                  role="group"
                  aria-label={write ? '写入原文' : '压缩处理后的文本'}
                >
                  {write ? (
                    <>
                      <p className="merged-write-meta">
                        {first.item.occurred_at ? '写入于 ' + first.preciseTime : '写入时间未记录'}{' '}
                        · {coverage || '覆盖时段未记录'}
                      </p>
                      <p>{first.item.text}</p>
                    </>
                  ) : (
                    group.map(({ item }) => (
                      <p key={item.id}>
                        {item.members ? item.text : item.text.replace(envelope, '')}
                      </p>
                    ))
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
