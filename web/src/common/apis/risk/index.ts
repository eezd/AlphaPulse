import type * as Risk from "./type"
import { request } from "@/http/axios"

export const getRiskSettingsApi = () => request<Risk.Settings>({ url: "/risk/settings", method: "get" })
export const saveRiskSettingsApi = (data: Risk.Settings) => request<null>({ url: "/risk/settings", method: "put", data })
export const enableRiskApi = (data: Risk.Settings) => request<null>({ url: "/risk/enable", method: "post", timeout: 650000, data })
export const getRiskOverviewApi = (hours: number) => request<Risk.Overview>({ url: "/risk/overview", method: "get", params: { hours } })
export const listRiskEventsApi = (params: Risk.EventQuery) => request<{ items: Risk.RiskEvent[], total: number }>({ url: "/risk/events", method: "get", params })
export const getRiskEventApi = (id: number) => request<{ event: Risk.RiskEvent, history: Risk.Analysis[] }>({ url: `/risk/events/${id}`, method: "get" })
export const listRiskAnalysesApi = (params: { news_id?: number, revision_no?: number, failed?: boolean }, notify = true) => request<Risk.Analysis[]>({ url: "/risk/analyses", method: "get", params, notify })
export const retryRiskAnalysisApi = (id: number) => request<null>({ url: `/risk/analyses/${id}/retry`, method: "post" })
