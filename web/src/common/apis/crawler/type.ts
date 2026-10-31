export interface ProcessStatus {
  state: "running" | "stopped" | "restarting"
  pid: number | null
  started_at: string | null
  next_run_at: string
}

export interface SourceInfo {
  code: string
  name: string
  region_code: string | null
  list_url: string
  enabled: boolean
  interval_seconds: number
  backfill_hours: number
  consecutive_failures: number
  last_success_at: string | null
  last_error: string | null
  paused_until: string | null
  supports_history: boolean
  history_target: string | null
  history_position: string | null
  history_done: boolean
  process: ProcessStatus | null
  today_new: number
  today_revised: number
  total_items: number
}

export interface SourcesInfo {
  manager_alive: boolean
  db_size_bytes: number
  sources: SourceInfo[]
  calendar_sources: CalendarSourceInfo[]
}

export interface CalendarSourceInfo {
  code: string
  name: string
  region_code: string
  worker_alive: boolean
  heartbeat_at: string | null
  schedule_checked_at: string | null
  schedule_success_at: string | null
  latest_result_at: string | null
  next_event_at: string | null
  overdue_count: number
  history_start: string | null
  history_end: string | null
  errors: string[]
}

export type ListSourcesResponseData = SourcesInfo

export interface CrawlLog {
  id: number
  source_code: string
  run_id: string | null
  ts: string
  level: string
  message: string
}

export interface SourcePatch {
  name?: string
  enabled?: boolean
  interval_seconds?: number
  /** ISO 时间表示回补到该时间；null 表示取消回补 */
  history_target?: string | null
}

export type PatchSourceResponseData = null

export type ListLogsResponseData = CrawlLog[]

export type RestartCrawlerResponseData = null

export interface RssInput { name: string, url: string, interval_seconds: number }
