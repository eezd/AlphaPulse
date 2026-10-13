import { http } from "@/http"

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
}

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

export interface NewsQuery {
  source?: string[]
  q?: string
  start?: string
  end?: string
  cursor?: string
  limit?: number
}

export interface NewsEvent {
  id: number
  source_code: string
  kind: "new" | "revised"
}

export function listNews(params: NewsQuery): Promise<{ items: NewsBrief[], next_cursor: string | null }> {
  // source 需要序列化为重复参数 source=a&source=b
  return http.get("/news", { params, paramsSerializer: { indexes: null } })
}

export function getNews(id: number): Promise<NewsDetail> {
  return http.get(`/news/${id}`)
}

export function listRevisions(id: number): Promise<Revision[]> {
  return http.get(`/news/${id}/revisions`)
}
