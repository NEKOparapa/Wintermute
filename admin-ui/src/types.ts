export type HistoryView =
  'conversation' | 'notification' | 'perception' | 'slice' | 'chat' | 'timeline'

export type RecordType = 'sms' | 'call_log' | 'contacts' | 'notion' | 'recording_list' | 'document'
export interface ImportedRecord {
  id: string
  record_type: RecordType
  source_name: string
  batch_id: string
  occurred_at: string
  received_at: string
  title: string | null
  external_id: string | null
  status: 'pending' | 'filtered' | 'processing' | 'processed' | 'failed'
  attempts: number
  error: string | null
}
export interface RecordContent {
  text: string
  format: 'text' | 'json'
  offset: number
  next_offset: number | null
  total_chars: number
}

export interface ChatFileRef {
  id: string
  name: string
}

export interface HistoryFile extends ChatFileRef {
  content_type?: string
  occurred_at?: string
  window_title?: string
}
export interface ScreenshotGroup {
  id: string
  app: string
  subject: string
  started_at: string
  ended_at: string
  frame_count: number
  change_count: number
}
export interface HistoryItem {
  id: string
  occurred_at: string | null
  role: string
  source: string
  text: string
  files: HistoryFile[]
  raw?: Record<string, unknown>
  filter_result?: 'filtered' | 'partial' | 'kept' | 'unprojected'
  context_messages?: ContextMessageRef[]
  pending_context?: boolean
  layer?: ActivityLayer | 'conversation' | 'notification'
  material_type?: 'slice.recorded' | 'slice.audio' | 'slice.image'
  processing?: 'ready' | 'retrying' | 'pending'
  members?: { id: string; occurred_at: string; device_id: string; layer: ActivityLayer }[]
  merged_summary?: {
    kind: 'ai' | 'rules'
    generated_at: string | null
    covered_from: string
    covered_until: string
  }
  context_write?: { covered_from: string | null; covered_until: string | null }
  summary_round?: SummaryRoundRef
  usage?: TimelineUsage
  group?: ScreenshotGroup | null
}
export interface HistoryPage {
  items: HistoryItem[]
  next_cursor: string | null
}

export interface ConnectorCall {
  id: string
  connector_id: string
  connector_name: string
  tool_type: 'mcp' | 'device' | 'cli'
  target: string
  tool: string
  arguments: Record<string, unknown>
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'timed_out' | 'expired'
  output: string
  error: string
  exit_code: number | null
  truncated: boolean
  timeout_seconds: number | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export type ActivityLayer = 'perception' | 'slice'

export interface ResultsPage<T> {
  items: T[]
  next_cursor: string | null
}

export interface OriginalRecord {
  id: string
  kind: string
  occurred_at: string
  source: string
  payload: Record<string, unknown>
  transcript: string | null
  files: HistoryFile[]
}
export interface OriginalDetail extends ResultsPage<OriginalRecord> {
  extraction: {
    status: 'ready' | 'no_change' | 'pending' | 'retrying' | 'not_recorded'
    text: string | null
    strategy: string | null
    source_count: number
  }
}

export interface PerceptionStages {
  record?: HistoryItem
  context_messages?: ContextMessageRef[]
  dwell_threshold_seconds?: number
  filter_reason?: 'wintermute_app' | 'mobile_home' | 'mobile_zero_duration' | 'location_nearby' | 'location_stale' | 'location_unresolved' | 'short_dwell' | null
  decisions: (
    | { kind: 'application' | 'title'; label: string; seconds: number; kept: boolean }
    | {
        kind: 'input'
        label: string
        kept: boolean
        action: 'kept' | 'normalized' | 'redacted' | 'empty' | 'too_long' | 'invalid_time'
      }
  )[]
  retained: unknown
  parts: {
    kind: 'observation' | 'visual'
    text: string
    tokens?: number
    delivered: boolean
    round_id: string | null
  }[]
  rounds: {
    id: string
    status: 'pending' | 'ready' | 'retrying' | 'disabled'
    input: string
    summary: string | null
    timeline_entry?: SummaryRoundRef | null
  }[]
  ai_enabled: boolean
  budget: number
  pending_size: number
}

export interface TimelineUsage {
  tokens: number
  tokenizer?: 'deepseek-v4'
  budget: number
  devices: string[]
  duration_seconds: number | null
}

export interface ContextMessageRef {
  id: string
  written_at: string | null
  activity_at?: string | null
  parts?: ('observation' | 'visual')[]
  usage?: TimelineUsage
}
export interface SummaryRoundRef {
  id: string
  occurred_at: string
  generated_at: string | null
  usage?: TimelineUsage
}
export interface TraceMember {
  id: string
  event_id: string | null
  title: string
  device_id: string
  occurred_at: string
  usage?: TimelineUsage
}
export interface TracePage extends ResultsPage<TraceMember> {
  round?: {
    input: string
    summary: string | null
    archived_text_only?: boolean
    previous_summary?: SummaryRoundRef | null
  }
  text: string | null
  written_at: string | null
}
