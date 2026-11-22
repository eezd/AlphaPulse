export type ContentStatus = "full" | "excerpt" | "metadata" | "extraction_failed"

export interface ResearchArticleMetadata {
  publisher_code: string
  publisher_name: string
  bylines: { name: string, profile_url: string | null, organization: string | null }[]
  canonical_url: string
  original_url: string | null
  language: string | null
  section: string | null
  content_status: ContentStatus
  access_status: "public" | "paywall" | "forbidden" | "unknown"
  attachments: { url: string, title: string | null, status: "link_only" }[]
}

export interface NewsBrief {
  analysis_admission?: { mode: "collect_only" | "archive" | "report", reason: string }
  research?: ResearchArticleMetadata | null
  id: number
  source_code: string
  url: string
  title: string
  summary: string
  excerpt: string
  author: string | null
  tags: string[]
  is_paywalled: boolean
  published_at_precision?: string | null
  published_date?: string | null
  published_at: string
  first_seen_at: string
  revision_no: number
  bookmarked?: boolean
  keywords?: { canonical: string }[]
  analysis?: { id: number, version_no: number, state: string, source_changed: boolean, importance_stars: number | null, importance_score: number | null, result: { summary: string } | null } | null
}

export interface NewsQuery {
  research_only?: boolean
  author?: string
  content_status?: ContentStatus
  processing_state?: string
  bookmarked?: boolean
  sort?: "asc" | "desc"
  ids?: number[]
  source?: string[]
  q?: string
  start?: string
  end?: string
  /** 从 1 开始 */
  page?: number
  limit?: number
}

export interface ListNewsResponseData { items: NewsBrief[], total: number }

export interface NewsDetail extends NewsBrief {
  content_html: string
  source_updated_at: string | null
  last_seen_at: string
  extra: Record<string, unknown>
}

export interface Revision {
  revision_no: number
  title: string
  summary: string
  content_html: string
  captured_at: string
}

export interface NewsEvent {
  id: number
  source_code: string
  kind: "new" | "revised"
}

export type GetNewsResponseData = NewsDetail

export type ListRevisionsResponseData = Revision[]
