import type * as Risk from "./type"
import { request } from "@/http/axios"

export const getRiskSettingsApi = () => request<Risk.Settings>({ url: "/risk/settings", method: "get" })
export const saveRiskSettingsApi = (data: Risk.Settings) => request<null>({ url: "/risk/settings", method: "put", data })
export const enableRiskApi = (data: Risk.Settings) => request<null>({ url: "/risk/enable", method: "post", timeout: 650000, data })
export const getRiskOverviewApi = (hours: number) => request<Risk.Overview>({ url: "/risk/overview", method: "get", params: { hours } })
export const listRiskEventsApi = (params: Risk.EventQuery) => request<{ items: Risk.RiskEvent[], total: number }>({ url: "/risk/summaries", method: "get", params })
export const getRiskEventApi = (id: number) => request<{ evidence: { id: number, kind: string, data: unknown, fetched_at: string }[], event: Risk.RiskEvent, history: Risk.Analysis[], versions: { id: number, revision: number, state: string, model: string, result: Risk.RiskResult | null, error: string | null, analyzed_at: string | null }[] }>({ url: `/risk/summaries/${id}`, method: "get" })
export const listRiskAnalysesApi = (params: { news_id?: number, revision_no?: number, failed?: boolean, stage?: string }, notify = true) => request<Risk.Analysis[]>({ url: "/risk/tasks", method: "get", params, notify })
export const retryRiskAnalysisApi = (id: number, stage = "grouping") => request<null>({ url: `/risk/tasks/${id}/retry`, method: "post", params: { stage } })

export const listNewsSummariesApi = (newsId: number, revision: number) => request<Risk.RiskEvent[]>({ url: `/risk/news/${newsId}/summaries`, method: "get", params: { revision_no: revision } })
export const createNewsSkillTemplatesApi = () => request<{ grouping_skill_id: number, analysis_skill_id: number, outlook_skill_id: number }>({ url: "/risk/skill-templates", method: "post" })
