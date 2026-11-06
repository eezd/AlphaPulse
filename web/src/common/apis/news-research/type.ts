export interface Keyword { canonical: string, aliases: string[], quote: string }
export interface ModelConfig { provider_id: number | null, model: string, reasoning_effort: string | null, skill_id: number | null }
export interface Config { index: ModelConfig, research: ModelConfig, concurrency: number }
export interface BatchProgress { id: number, phase: string, cutoff_at: string, packages_total: number, packages_done: number, packages_failed: number, packages_deferred: number, events_total: number, events_done: number, events_failed: number, events_deferred: number }
export interface Settings { batch?: BatchProgress | null, config: Partial<Config>, enabled: boolean, start_at: string }
export interface Coverage { start_at: string, checked_at: string, total: number, unprocessed: number, missing: number, stale: number, pending: number, processing: number, failed: number, completed: number, outside: { unprocessed: number, stale: number, pending: number, processing: number, failed: number } }
export interface Package { id: number, news_id: number, title: string, url: string, state: string, error: string | null, keywords: Keyword[], events: { id: number, title: string, reason: string, quote: string }[] }
export interface Point { time_precision: "minute" | "day" | "month" | "unknown", planned_precision: "minute" | "day" | "month" | "unknown", package_ids: number[], time_text: string, description: string, occurred_at: string | null, planned_at: string | null, reported_at: string | null, period_text: string, evidence_refs: { article_id: number, quote: string }[] }
export interface Result { title: string, summary: string, facts: string, reasoning: string, uncertainty: string, opinions: string, counter_evidence: string, watchpoints: string, asset_impacts: string, importance_stars: number, risk_stars: number, confidence: string, verification: string, timeline_points: Point[] }
export interface Event { id: number, title: string, state: string, revision: number, error: string | null, tags: string[], result: Result | null }
export interface EventDetail extends Event { packages: Package[], references: { event_id: number, title: string, version_id: number, role: string, reason: string, changed: boolean }[], versions: { id: number, revision: number, state: string, result: Result | null, error: string | null, usage: unknown, evidence: unknown }[] }
export interface PackageDetail extends Package { article: { title: string, summary: string, content: string }, attempts: { id: number, stage: string, state: string, error: string | null, output: unknown, usage: unknown }[] }

export interface VocabularyEntry { canonical: string, aliases: string[] }
export interface Vocabulary { revision: number, entries: VocabularyEntry[] }
export interface VersionDetail { evidence: unknown, usage: unknown, id: number, event_id: number, revision: number, state: string, result: Result, packages: { id: number, news_id: number }[], articles: { id: number, title: string, summary: string, content: string }[], references: EventDetail["references"] }
