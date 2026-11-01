import type * as Risk from "./type"
import { request } from "@/http/axios"

export const getRiskSettingsApi = () => request<Risk.Settings>({ url: "/risk/settings", method: "get" })
export const saveRiskSettingsApi = (data: Risk.Settings) => request<null>({ url: "/risk/settings", method: "put", data })
export const enableRiskApi = (data: Risk.Settings) => request<null>({ url: "/risk/enable", method: "post", timeout: 650000, data })
export const getRiskOverviewApi = (hours: number) => request<Risk.Overview>({ url: "/risk/overview", method: "get", params: { hours } })
export const listRiskEventsApi = (params: Risk.EventQuery) => request<{ items: Risk.RiskEvent[], total: number }>({ url: "/risk/summaries", method: "get", params })
export const getRiskEventApi = (id: number) => request<{ related_events?: { id: number, reason: string }[], corrections?: { id: number, result: unknown }[], grouping_versions?: { id: number, state: string, error: string | null }[], evidence: { id: number, kind: string, data: unknown, fetched_at: string }[], event: Risk.RiskEvent, history: Risk.Analysis[], versions: { id: number, revision: number, state: string, model: string, result: Risk.RiskResult | null, error: string | null, analyzed_at: string | null }[] }>({ url: `/risk/summaries/${id}`, method: "get" })
export const listRiskAnalysesApi = (params: { news_id?: number, revision_no?: number, failed?: boolean, stage?: string }, notify = true) => request<Risk.Analysis[]>({ url: "/risk/tasks", method: "get", params, notify })
export const retryRiskAnalysisApi = (id: number, stage = "grouping") => request<null>({ url: `/risk/tasks/${id}/retry`, method: "post", params: { stage } })

export const listNewsSummariesApi = (newsId: number, revision: number) => request<Risk.RiskEvent[]>({ url: `/risk/news/${newsId}/summaries`, method: "get", params: { revision_no: revision } })
export const createNewsSkillTemplatesApi = (refreshExisting = false) => request<{ grouping_skill_id: number, analysis_skill_id: number, outlook_skill_id: number }>({ url: "/risk/skill-templates", method: "post", data: { refresh_existing: refreshExisting } })
export const listNewsEventsApi = (page = 1) => request<{ items: Risk.NewsEvent[], total: number }>({ url: "/risk/events", method: "get", params: { page } })
export const getNewsEventApi = (id: number) => request<Risk.EventDetail>({ url: `/risk/events/${id}`, method: "get" })
export const previewReprocessApi = (data: Risk.ReprocessRange) => request<Risk.ReprocessPreview>({ url: "/risk/reprocess/preview", method: "post", data })
export const startReprocessApi = (data: { preview_id: string }) => request<{ id: number }>({ url: "/risk/reprocess", method: "post", data })
export const listReprocessApi = () => request<Risk.ReprocessJob[]>({ url: "/risk/reprocess", method: "get" })
export const cancelReprocessApi = (id: number) => request<null>({ url: `/risk/reprocess/${id}/cancel`, method: "post" })
export const retryReprocessApi = (id: number) => request<null>({ url: `/risk/reprocess/${id}/retry`, method: "post" })

export const saveRiskConcurrencyApi = (max_concurrency: number) => request<{ max_concurrency: number }>({ url: "/risk/settings/concurrency", method: "patch", data: { max_concurrency } })

export const listRiskQueueApi = (params: { state?: string, stage?: string, origin?: string, job_id?: number, page?: number }) => request<Risk.Queue>({ url: "/risk/queue", method: "get", params })
export const getReprocessDetailApi = (id: number, page = 1) => request<Risk.ReprocessJob & { items: { id: number, news_id: number, title: string, url: string, mode: string, state: string, error: string | null }[] }>({ url: `/risk/reprocess/${id}`, method: "get", params: { page } })
