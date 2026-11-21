import { historyPage } from './api'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { HistoryItem, HistoryView } from './types'

export interface PageState {
  items: HistoryItem[]
  next: string | null
  requested: string | null
  loading: boolean
  error: string
}

export const emptyPage: PageState = {
  items: [],
  next: null,
  requested: null,
  loading: false,
  error: '',
}

export function useHistoryPages(view: HistoryView) {
  const [pages, setPages] = useState(
    () => new Map<string | null, PageState>([[null, { ...emptyPage, loading: true }]]),
  )
  const pending = useRef(new Map<string | null, symbol>())
  const loaded = useRef<HistoryItem[]>([])
  useEffect(() => {
    loaded.current = pages.get(null)?.items ?? []
  }, [pages])
  const fetchPage = useCallback(
    (groupId: string | null, cursor: string | null, background = false) => {
      if (pending.current.has(groupId)) return
      const token = Symbol()
      pending.current.set(groupId, token)
      return historyPage(view, groupId, cursor)
        .then(async (page) => {
          if (pending.current.get(groupId) !== token) return
          if ((view === 'perception' || view === 'slice' || view === 'timeline') && background) {
            // Recognition can complete on an older page while the user has it open.
            const prior = loaded.current
            const unfinished = new Set(
              prior
                .filter((item) => {
                  const payload = item.raw?.payload as { image_recognition?: string } | undefined
                  if (view === 'timeline' || view === 'slice') return true
                  return payload?.image_recognition && payload.image_recognition !== 'ready'
                })
                .map((item) => item.id),
            )
            let chunk = page
            while (unfinished.size) {
              for (const item of chunk.items) unfinished.delete(item.id)
              if (!unfinished.size || !chunk.next_cursor || page.items.length >= prior.length) break
              chunk = await historyPage(view, null, chunk.next_cursor)
              if (pending.current.get(groupId) !== token) return
              page = { items: [...page.items, ...chunk.items], next_cursor: chunk.next_cursor }
            }
          }
          const items =
            view === 'timeline'
              ? page.items
                  .filter((item) => item.text.trim())
                  .map((item) => ({ ...item, files: [] }))
              : page.items
          const refresh = groupId === null && cursor === null
          if (refresh) pending.current.clear()
          setPages((previous) => {
            const next = refresh ? new Map<string | null, PageState>() : new Map(previous)
            const rows = new Map<string, HistoryItem>()
            if (cursor) for (const row of previous.get(groupId)?.items ?? []) rows.set(row.id, row)
            for (const row of items) rows.set(row.id, row)
            if (
              background &&
              view !== 'timeline' &&
              view !== 'slice' &&
              page.next_cursor &&
              items.length
            ) {
              const prior = previous.get(groupId)
              const last = items.at(-1)!
              const overlap = prior?.items.findIndex(
                (row) =>
                  row.id === last.id &&
                  (view === 'perception' || row.text === last.text) &&
                  row.source === last.source &&
                  row.occurred_at === last.occurred_at,
              )
              if (prior && overlap !== undefined && overlap >= 0)
                return next.set(groupId, {
                  ...prior,
                  items: [...items, ...prior.items.slice(overlap + 1)],
                  error: '',
                  loading: false,
                })
            }
            return next.set(groupId, {
              items: [...rows.values()],
              next: page.next_cursor,
              requested: cursor,
              error: '',
              loading: false,
            })
          })
        })
        .catch((error) => {
          if (pending.current.get(groupId) !== token) return
          setPages((previous) =>
            new Map(previous).set(groupId, {
              ...(previous.get(groupId) ?? emptyPage),
              loading: false,
              requested: cursor,
              error:
                error instanceof Error && error.message
                  ? error.message.replace(/^.*?Error: /, '')
                  : '读取历史失败，请重试。',
            }),
          )
        })
        .finally(() => {
          if (pending.current.get(groupId) === token) pending.current.delete(groupId)
        })
    },
    [view],
  )
  useEffect(() => {
    void fetchPage(null, null)
    const requests = pending.current
    function refreshVisible() {
      if (document.visibilityState === 'visible') void fetchPage(null, null, true)
    }
    if (!['chat', 'perception', 'slice', 'timeline'].includes(view)) return () => requests.clear()
    const timer = window.setInterval(refreshVisible, 5000)
    window.addEventListener('focus', refreshVisible)
    document.addEventListener('visibilitychange', refreshVisible)
    return () => {
      requests.clear()
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshVisible)
      document.removeEventListener('visibilitychange', refreshVisible)
    }
  }, [fetchPage, view])
  const read = useCallback(
    (groupId: string | null, cursor: string | null) => {
      if (pending.current.has(groupId)) return
      setPages((previous) =>
        new Map(previous).set(groupId, {
          ...(previous.get(groupId) ?? emptyPage),
          loading: true,
          requested: cursor,
          error: '',
        }),
      )
      void fetchPage(groupId, cursor)
    },
    [fetchPage],
  )
  return { pages, read }
}
