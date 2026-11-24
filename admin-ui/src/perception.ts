import type { HistoryItem } from './types'

export const labels: Record<string, string> = {
  'mobile.location': '地点',
  'mobile.app_activity': '移动端活动',
  domain: '网站',
  title: '标题',
  window_title: '窗口',
  foreground_app: '应用',
  activity: '活动',
  duration_seconds: '停留秒数',
  started_at: '开始时间',
  ended_at: '结束时间',
}

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function text(value: unknown) {
  return typeof value === 'string' ? value : ''
}

export function duration(seconds: number) {
  seconds = Math.trunc(seconds)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds / 60) % 60
  const rest = seconds % 60
  return [
    hours && hours + ' 小时',
    minutes && minutes + ' 分钟',
    (rest || !seconds) && rest + ' 秒',
  ]
    .filter(Boolean)
    .join(' ')
}

// Earlier visits have a frozen text snapshot. Format only the duration in its header.
export function formatVisitText(value: string) {
  return value
    .replace(/^([^\n]+（\d+)\.\d+(?=s）\r?\n)/, '$1')
    .replace(/^([^\n]+ \d\d:\d\d:\d\d–\d\d:\d\d:\d\d \d+)\.\d+(?= 秒\r?\n\s*内容：)/, '$1')
}

export function perceptionLabel(item: HistoryItem) {
  const payload = record(item.raw?.payload)
  const value = record(payload.value)
  const key = text(payload.state_key)
  return {
    title: text(value.foreground_app) || labels[key] || key || text(item.raw?.type) || '设备观测',
    subject: text(value.window_title) || text(value.title),
    source: text(item.raw?.device_id) || item.source,
    duration: typeof value.duration_seconds === 'number' ? duration(value.duration_seconds) : '',
  }
}
