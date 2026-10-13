import { http } from "@/http"

export interface ProcessStatus {
  state: "running" | "stopped" | "restarting"
  pid: number | null
  started_at: string | null
  next_run_at: string
}

export interface SourceInfo {
  code: string
  name: string
  list_url: string
  enabled: boolean
  interval_seconds: number
  backfill_hours: number
  consecutive_failures: number
  last_success_at: string | null
  last_error: string | null
  paused_until: string | null
  process: ProcessStatus | null
  today_new: number
  today_revised: number
  total_items: number
}

export interface SourcesInfo {
  manager_alive: boolean
  db_size_bytes: number
  sources: SourceInfo[]
}

export interface CrawlLog {
  id: number
  source_code: string
  run_id: string | null
  ts: string
  level: string
  message: string
}

export function listSources(): Promise<SourcesInfo> {
  return http.get("/crawler/sources")
}

export function patchSource(code: string, body: { enabled?: boolean, interval_seconds?: number }): Promise<void> {
  return http.patch(`/crawler/sources/${code}`, body)
}

export function listLogs(source: string, limit = 200): Promise<CrawlLog[]> {
  return http.get("/crawler/logs", { params: { source, limit } })
}

export function restartCrawler(source: string): Promise<void> {
  return http.post("/crawler/restart", { source })
}
