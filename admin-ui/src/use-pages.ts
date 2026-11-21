import { useCallback, useEffect, useRef, useState } from 'react'
import type { ResultsPage } from './types'

type WithId = { id: string }
export function usePages<T extends WithId, P extends ResultsPage<T>>(
  fetch: (cursor?: string) => Promise<P>,
) {
  const [page, setPage] = useState<P | null>(null)
  const [items, setItems] = useState<T[]>([])
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [failedCursor, setFailedCursor] = useState<string>()
  const request = useRef<symbol | null>(null)
  const fetchPage = useCallback(
    async (cursor?: string) => {
      if (request.current) return
      const token = Symbol()
      request.current = token
      try {
        const result = await fetch(cursor)
        if (request.current !== token) return
        setPage(result)
        setItems((previous) => {
          const rows = new Map<string, T>()
          if (cursor) for (const item of previous) rows.set(item.id, item)
          for (const item of result.items) rows.set(item.id, item)
          return [...rows.values()]
        })
      } catch (e) {
        if (request.current !== token) return
        setFailedCursor(cursor)
        setError(e instanceof Error ? e.message.replace(/^.*?Error: /, '') : '读取失败，请重试。')
      } finally {
        if (request.current === token) {
          request.current = null
          setBusy(false)
        }
      }
    },
    [fetch],
  )
  function read(cursor?: string) {
    if (request.current) return
    setBusy(true)
    setError('')
    void fetchPage(cursor)
  }
  useEffect(() => {
    let mounted = true
    queueMicrotask(() => {
      if (mounted) void fetchPage()
    })
    return () => {
      mounted = false
      request.current = null
    }
  }, [fetchPage])
  return {
    page,
    items,
    busy,
    error,
    next: page?.next_cursor,
    failedCursor,
    read,
  }
}
