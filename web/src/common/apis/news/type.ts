export interface NewsBrief {
  id: number
  source_code: string
  url: string
  title: string
  summary: string
  excerpt: string
  author: string | null
  tags: string[]
  is_paywalled: boolean
  published_at: string
  first_seen_at: string
  revision_no: number
  risk?: { state: string, stars: number | null, summary: string | null, verification: string | null } | null
}

export interface NewsQuery {
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
