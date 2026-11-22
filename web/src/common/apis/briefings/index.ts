import type {
  AiQueue,
  AiResetResult,
  AiWaitTarget
} from "./type"
import { request } from "@/http/axios"

export type { AiQueue, AiQueueItem, AiWaitTarget } from "./type"

export type Kind = "morning" | "evening" | "intraday" | "daily"
export interface Region {
  code: string
  name: string
}
export interface Keyword {
  canonical: string
  aliases: string[]
  kind: string
  quote: string
}
export interface Scenario {
  condition: string
  consequence: string
}
export interface AnalysisResult {
  summary: string
  facts: { text: string, quote: string }[]
  impact: string
  importance_score: number
  importance_reason: string
  primary_region: Region
  related_regions: Region[]
  keywords: Keyword[]
  assets: {
    name: string
    symbol: string | null
    asset_class: string
    venue: string | null
  }[]
  uncertainties: string[]
  scenarios: Scenario[]
}
export interface Analysis {
  id: number
  news_id: number
  title: string
  version_no: number
  state: string
  lane: string
  source_revision: number | null
  source_changed: boolean
  time_basis: string
  effective_at: string
  keywords: Keyword[]
  importance_score: number | null
  importance_stars: number | null
  result: AnalysisResult | null
  error: string | null
  usage: unknown
  rules: Record<string, unknown> | null
  input?: Record<string, unknown> | null
  versions?: { id: number, version_no: number, state: string }[]
}
export interface ReportEvent {
  title: string
  importance_score: number
  facts: string[]
  source_analysis_ids: number[]
  market_changes: string[]
  impact: string
  suggestions: string[]
  watch_points: string[]
  scenarios: Scenario[]
  uncertainties: string[]
}
export interface ReportOutput {
  title: string
  summary: string
  sections: { region: Region, summary: string, events: ReportEvent[] }[]
  highlight_analysis_ids: number[]
}
export interface Coverage {
  total?: number
  available?: number
  missing?: number
  missing_rate?: number
  missing_news_ids?: number[]
  reason?: string
  missing_report_kind?: string
  periods?: { report_id: number, kind: Kind, coverage: Coverage }[]
}
export interface Report {
  id: number
  kind: Kind
  report_date: string
  version_no: number
  lane: string
  state: string
  title: string | null
  summary?: string | null
  window_start: string
  window_end: string
  due_at: string
  next_check_at: string
  first_check_at: string | null
  cutoff_at: string | null
  coverage: Coverage | null
  error: string | null
  finished_at: string | null
}
export interface MarketSnapshot {
  market: string
  symbol: string
  status: string
  [key: string]: unknown
}
export interface ReportDetail extends Report {
  output: ReportOutput | null
  input: {
    analysis_ids: number[]
    news_ids?: number[]
    dependencies?: { report_id: number, kind: Kind, version_no: number }[]
  } | null
  market: MarketSnapshot[] | null
  rules: Record<string, unknown> | null
  usage: {
    input_tokens: number
    output_tokens: number
    unknown_calls: number
    known_calls: number
    reused_steps: number
  }
  sources: {
    analysis_id: number
    news_id: number
    source_revision: number
    title: string
    url: string
  }[]
  versions: { id: number, version_no: number, state: string }[]
  parts: {
    id: number
    part_key: string
    stage: string
    state: string
    reused_from: number | null
    usage: unknown
    error: string | null
  }[]
}
export interface ModelConfig {
  provider_id: number | null
  model: string
  skill_id: number | null
  reasoning_effort: string | null
  max_tokens: number
}
export interface BriefingConfig {
  analysis: ModelConfig
  period: ModelConfig
  daily: ModelConfig
  concurrency: number
  backfill_start: string | null
}
export interface TelegramStatus {
  enabled: boolean
  token_configured: boolean
  chat_id_masked: string | null
}
export interface Settings {
  enabled: boolean
  config: Partial<BriefingConfig>
  control_revision: number
  enabled_at: string | null
  history_start: string | null
  history_end: string | null
  history_cursor: string | null
  history_enrolled: boolean
  telegram: TelegramStatus
}
export interface QueueCoverage {
  news_total: number
  not_enrolled: number
  analyses: { state: string, lane: string, count: number }[]
  reports: { state: string, lane: string, count: number }[]
  historical_windows_expected: number
  history_start: string
  history_end: string
  history_cursor: string | null
  history_enrolled: boolean
}
export interface Notification {
  id: number
  incident: string
  state: string
  message: string
  attempts: number
  error: string | null
  created_at: string
  sent_at: string | null
}
export interface Page<T> {
  items: T[]
  total: number
}
export const kinds: Record<Kind, string> = {
  morning: "早报",
  evening: "晚报",
  intraday: "盘中报",
  daily: "日报"
}
export const states: Record<string, string> = {
  pending: "待处理",
  processing: "处理中",
  done: "已完成",
  failed: "失败",
  interrupted: "已中断",
  blocked: "已阻塞",
  waiting_backfill: "等待历史分析",
  waiting_coverage: "等待资料",
  sent: "已送达",
  sending: "发送中",
  unknown: "投递结果未知"
}
export function getSettingsApi() {
  return request<Settings>({ url: "briefings/settings" })
}
export function saveSettingsApi(
  data: BriefingConfig & { base_revision: number }
) {
  return request<Settings>({ url: "briefings/settings", method: "put", data })
}
export function enableApi(base_revision: number) {
  return request<Settings>({
    url: "briefings/enable",
    method: "post",
    data: { base_revision }
  })
}
export function pauseApi(base_revision: number) {
  return request<Settings>({
    url: "briefings/pause",
    method: "post",
    data: { base_revision }
  })
}
export function getCoverageApi() {
  return request<QueueCoverage>({ url: "briefings/coverage" })
}
export function listAnalysesApi(params: Record<string, unknown> = {}) {
  return request<Page<Analysis>>({ url: "briefings/analyses", params })
}
export function getAnalysisApi(id: number) {
  return request<Analysis>({ url: `briefings/analyses/${id}` })
}
export function retryAnalysisApi(id: number) {
  return request<{ id: number }>({
    url: `briefings/analyses/${id}/retry`,
    method: "post"
  })
}
export function listReportsApi(params: Record<string, unknown> = {}) {
  return request<Page<Report>>({ url: "briefings/reports", params })
}
export function getReportApi(id: number) {
  return request<ReportDetail>({ url: `briefings/reports/${id}` })
}
export function generateReportApi(data: {
  kind: Kind
  report_date: string
  request_id: string
  regenerate: boolean
}) {
  return request<{ id: number, state: string, version_no: number }>({
    url: "briefings/reports/generate",
    method: "post",
    data
  })
}
export function getAiQueueApi() {
  return request<AiQueue>({ url: "briefings/queue" })
}
export function resetAiWaitsApi(targets: AiWaitTarget[]) {
  return request<AiResetResult>({
    url: "briefings/queue/reset-waits",
    method: "post",
    data: { targets }
  })
}
export function saveTelegramApi(data: {
  base_revision: number
  enabled: boolean
  chat_id?: string
  token?: string
  clear_token: boolean
}) {
  return request<TelegramStatus & { control_revision: number }>({
    url: "briefings/telegram",
    method: "put",
    data
  })
}
export function listNotificationsApi(page = 1) {
  return request<Page<Notification>>({
    url: "briefings/notifications",
    params: { page }
  })
}
export function initializeSkillTemplatesApi(base_revision: number) {
  return request<{ stage: "analysis" | "period" | "daily", skill_id: number }[]>({
    url: "briefings/skill-templates/initialize",
    method: "post",
    data: { base_revision }
  })
}
export function getSkillTemplatesApi() {
  return request<{ stage: string, name: string, content: string }[]>({
    url: "briefings/skill-templates"
  })
}
