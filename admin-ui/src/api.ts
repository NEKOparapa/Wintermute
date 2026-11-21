import type {
  ConnectorCall,
  HistoryPage,
  HistoryView,
  ImportedRecord,
  OriginalDetail,
  PerceptionStages,
  RecordContent,
  RecordType,
  ResultsPage,
  TracePage,
} from './types'

const conversation = document.getElementById('record-browser')?.dataset.conversation ?? ''
const base = '/admin/records/c-' + encodeURIComponent(conversation)

async function read<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const query = new URLSearchParams(params)
  const response = await fetch(base + path + (query.size ? '?' + query : ''))
  if (response.redirected && new URL(response.url).pathname === '/admin/login') {
    window.location.assign('/admin/login')
    throw new Error('登录已过期。')
  }
  if (!response.ok)
    throw new Error(response.status === 404 ? '找不到这条记录。' : '读取记录失败，请重试。')
  return response.json()
}

function pageParams(cursor?: string | null): Record<string, string> {
  return cursor ? { cursor, limit: '40' } : { limit: '40' }
}

export function historyPage(view: HistoryView, group: string | null, cursor: string | null) {
  const params = pageParams(cursor)
  if (group)
    return read<HistoryPage>('/history/screenshot-groups/' + encodeURIComponent(group), params)
  params.view = view === 'timeline' ? 'merged' : view
  return read<HistoryPage>('/history', params)
}

export function fileUrl(id: string) {
  return base + '/history/files/' + encodeURIComponent(id)
}

export function perceptionStages(id: string, includeRecord: boolean) {
  return read<PerceptionStages>(
    '/history/perceptions/' + encodeURIComponent(id) + '/stages',
    includeRecord ? { record: '1' } : {},
  )
}

export function traceMembers(id: string, cursor?: string) {
  return read<TracePage>('/history/traces/' + encodeURIComponent(id), pageParams(cursor))
}

export function originalSource(id: string, activity?: boolean, cursor?: string) {
  const params = pageParams(cursor)
  if (activity) params.activity = '1'
  return read<OriginalDetail>('/history/compression/sources/' + encodeURIComponent(id), params)
}

export async function recordItems(
  cursor?: string,
  type?: RecordType,
): Promise<ResultsPage<ImportedRecord>> {
  const params = pageParams(cursor)
  if (type) params.record_type = type
  const page = await read<ResultsPage<Omit<ImportedRecord, 'id'> & { item_id: string }>>(
    '/items',
    params,
  )
  return { ...page, items: page.items.map(({ item_id, ...item }) => ({ ...item, id: item_id })) }
}

export function recordContent(id: string, offset: number) {
  return read<RecordContent>('/items/' + encodeURIComponent(id), { offset: String(offset) })
}

export function connectorHistory(cursor?: string) {
  return read<ResultsPage<ConnectorCall>>('/connectors', cursor ? { cursor } : {})
}
