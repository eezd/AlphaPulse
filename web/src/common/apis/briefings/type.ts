export interface AiWaitTarget { task_id: string, wait_id: string }
export interface AiQueueItem {
  claim: { kind: "analysis" | "report" | "report_part", id: number }
  analysis_id?: number | null
  report?: { id: number, kind: string, report_date: string, version_no: number } | null
  task_id: string | null
  stage: "analysis" | "report"
  news: { id: number, title: string, source_code: string, published_at: string | null, url: string }[]
  started_at: string
  progress: null | {
    phase: string
    operation: string | null
    updated_at: string
    model: string | null
    reasoning_effort: string | null
    round: number | null
    tool: string | null
    current_news_id: number | null
    retry_attempt: number
    retry_at: string | null
    wait_id: string | null
  }
  can_retry_now: boolean
}
export interface AiQueue {
  checked_at: string
  progress_status: "available" | "stale" | "unavailable"
  worker_heartbeat_at: string | null
  items: AiQueueItem[]
}
export interface AiResetResult {
  scheduled: AiWaitTarget[]
  already_requested: AiWaitTarget[]
  skipped: (AiWaitTarget & { reason: "not_waiting" | "stale" })[]
}
