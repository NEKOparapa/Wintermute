import { createRoot } from 'react-dom/client'
import { HistoryList } from './history-list'
import { RecordTimeline } from './record-timeline'
import { TerminalToolHistory } from './terminal-tool-history'
import type { HistoryView } from './types'

const root = document.getElementById('record-browser')
if (root) {
  const { view, label } = root.dataset
  createRoot(root).render(
    view === 'record' ? (
      <RecordTimeline />
    ) : view === 'connectors' ? (
      <TerminalToolHistory />
    ) : (
      <HistoryList view={view as HistoryView} label={label ?? ''} />
    ),
  )
}
