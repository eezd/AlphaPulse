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
  enabled: boolean
  provider_id: number | null
  model: string
  second_provider_id: number | null
  second_model: string
  third_provider_id: number | null
  third_model: string
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
export interface Reading { id: number, revision: number, priority: number, reason: string }
export interface Trend { summary: string, conditions: string, counter_evidence: string, watchpoints: string }
export interface Briefing {
  reading: Reading[]
  expansion: Reading[]
  as_of: string
  synthesis: { summary: string, market: string, short_term: Trend, medium_term: Trend } | null
  inputs: { id: number, revision: number, category: string, tags: string[], result: RiskResult & { title: string } }[]
}
export interface Overview {
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
