import type { Effort } from "../settings/type"

export interface RiskResult {
  summary: string
  reasoning: string
  uncertainty: string
  evidence?: string
  direction: string
  verification: string
  confidence: string
  risk_stars: number
  importance_stars: number
  facts?: string
  timeline?: string
  asset_impacts?: string
  opinions?: string
  counter_evidence?: string
  watchpoints?: string
  claims?: { statement: string, verdict: string, evidence_refs: { article_id: number, quote: string }[] }[]
  related_packages?: { id: number, relation: string, reason: string }[]
  evidence_refs?: { article_id: number, quote: string }[]
}
export interface RiskEvent {
  main_event_id?: number | null
  replacement_ids?: number[]
  tags: string[]
  content_kind: string
  importance_stars: number
  article_count: number
  id: number
  title: string
  category: string
  entities: string[]
  risk_stars: number
  peak_stars: number
  result: RiskResult | null
  state: string
  analyzed_at: string | null
  revision: number
  published_revision: number
  error: string | null
  published_at: string
  updated_at: string
}
export interface Settings {
  max_concurrency: number
  enabled: boolean
  provider_id: number | null
  model: string
  second_provider_id: number | null
  second_model: string
  third_provider_id: number | null
  third_model: string
  reasoning_effort: Effort | null
  second_reasoning_effort: Effort | null
  third_reasoning_effort: Effort | null
  grouping_skill_id: number | null
  analysis_skill_id: number | null
  outlook_skill_id: number | null
  start_at: string | null
}
export interface Analysis {
  id: number
  news_id: number
  state: string
  result: RiskResult | null
  input: { title: string, source: string, url: string, published_at: string, progress_at: string, revision_no: number }
  model: string | null
  error: string | null
  analyzed_at: string | null
  historical: boolean
}
export interface Reading { kind?: "package" | "event", id: number, revision: number, priority: number, reason: string }
export interface Trend { summary: string, conditions: string, counter_evidence: string, watchpoints: string }
export interface Briefing {
  reading: Reading[]
  expansion: Reading[]
  as_of: string
  synthesis: { summary: string, market: string, short_term: Trend, medium_term: Trend } | null
  inputs: { kind?: "package" | "event", id: number, revision: number, category: string, tags: string[], result: RiskResult & { title: string } }[]
}
export interface Overview {
  concurrency: { limit: number, running: number | null, queued: number, stages: Record<string, number>, updated_at: string | null }
  briefing: Briefing | null
  stage2: Record<string, number>
  stage3?: { state: string, pending: boolean, heartbeat: string | null }
  as_of: string
  counts: Record<string, number>
  coverage: { total: number, done: number, skipped: number, failed: number, pending: number, processing: number }
  scenario: { direction: string, summary: string, conditions: string, counter_factors: string, uncertainty: string, event_ids: number[] } | null
  snapshot_at: string | null
  quotes: { at: string, BTCUSDT?: { lastPrice: string, priceChangePercent: string }, ETHUSDT?: { lastPrice: string, priceChangePercent: string } } | null
  stale: boolean
  error: string | null
  heartbeat: string | null
}
export interface EventQuery { tag?: string, importance?: number, hours?: number, stars?: number, q?: string, category?: string, asset?: string, start?: string, end?: string, page?: number }

export interface TimelinePoint {
  occurred_at: string | null
  time_precision: string
  time_text: string
  reported_at: string | null
  description: string
  verification: string
  package_id: number
  package_revision: number
  evidence_refs: { article_id: number, quote: string }[]
}
export interface NewsEvent {
  id: number
  title: string
  state: string
  error: string | null
  result: { summary: string, importance_stars: number, risk_stars: number, counter_evidence: string, timeline_points: TimelinePoint[], uncertainty: string, opinions: string, watchpoints: string } | null
}
export interface EventDetail extends NewsEvent {
  packages: RiskEvent[]
  versions: { id: number, revision: number, state: string, error: string | null, result: NewsEvent["result"] }[]
}
export interface ReprocessRange { start: string, end: string, modes: ("missing" | "review")[] }
export interface ReprocessPreview { articles: number, packages: number, events: number, total: number, missing: number, review: number, preview_id: string, models: Record<string, { model: string, reasoning_effort: string | null }> }
export interface ReprocessJob { request?: { start: string, end: string, modes: string[] }, counts?: Record<string, number>, stages?: Record<string, Record<string, number>>, id: number, state: string, total: number, completed: number, failures: { stage: "grouping" | "analysis" | "events" | "outlook", id: number, error: string }[] }

export interface QueueItem { key: string, stage: string, object_id: number, title: string, job_id: number | null, origin: string, state: string, phase: string, model: string | null, reasoning_effort: string | null, started_at: string | null, updated_at: string | null, finished_at: string | null, url: string | null, round: number | null, tool: string | null, error: string | null }
export interface Queue {
  items: QueueItem[]
  total: number
  as_of: string
  worker_alive: boolean
  enabled: boolean
  limit: number
  concurrency: { running: number, stages: Record<string, number> } | null
}
