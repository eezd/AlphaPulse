export interface RiskResult {
  summary: string
  reasoning: string
  uncertainty: string
  evidence: string
  direction: string
  verification: string
  confidence: string
  stars: number
}
export interface RiskEvent {
  id: number
  title: string
  category: string
  entities: string[]
  stars: number
  peak_stars: number
  result: RiskResult
  published_at: string
  updated_at: string
}
export interface Settings { enabled: boolean, provider_id: number | null, model: string, start_at: string }
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
export interface Overview {
  as_of: string
  counts: Record<string, number>
  coverage: { total: number, done: number, failed: number, pending: number, processing: number }
  scenario: { direction: string, summary: string, conditions: string, counter_factors: string, uncertainty: string, event_ids: number[] } | null
  snapshot_at: string | null
  quotes: { at: string, BTCUSDT?: { lastPrice: string, priceChangePercent: string }, ETHUSDT?: { lastPrice: string, priceChangePercent: string } } | null
  stale: boolean
  error: string | null
  heartbeat: string | null
}
export interface EventQuery { hours?: number, stars?: number, q?: string, start?: string, end?: string, page?: number }
