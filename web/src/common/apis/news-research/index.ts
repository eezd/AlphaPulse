import type * as Research from "./type"
import { request } from "@/http/axios"

export const getSettingsApi = () => request<Research.Settings>({ url: "/risk/settings", method: "get" })
export const saveSettingsApi = (data: Research.Config) => request<null>({ url: "/risk/settings", method: "put", data })
export const enableApi = (start_at: string) => request<Research.Coverage>({ url: "/risk/enable", method: "post", data: { start_at } })
export const getCoverageApi = (start_at: string) => request<Research.Coverage>({ url: "/risk/coverage", method: "get", params: { start_at } })
export const pauseApi = () => request<null>({ url: "/risk/pause", method: "post" })
export const createSkillsApi = () => request<{ index: number, research: number }>({ url: "/risk/skill-templates", method: "post" })
export const listEventsApi = (q: string, page: number) => request<{ items: Research.Event[], total: number }>({ url: "/risk/events", method: "get", params: { q, page } })
export const getEventApi = (id: number) => request<Research.EventDetail>({ url: `/risk/events/${id}`, method: "get" })
export const retryEventApi = (id: number) => request<null>({ url: `/risk/events/${id}/retry`, method: "post" })
export const listPackagesApi = (q: string, page: number) => request<{ items: Research.Package[], total: number }>({ url: "/risk/packages", method: "get", params: { q, page } })
export const getPackageApi = (id: number) => request<Research.PackageDetail>({ url: `/risk/packages/${id}`, method: "get" })
export const retryPackageApi = (id: number) => request<null>({ url: `/risk/packages/${id}/retry`, method: "post" })
export const listNewsPackagesApi = (id: number, revision: number) => request<Research.Package[]>({ url: `/risk/news/${id}/packages`, method: "get", params: { revision_no: revision } })
export const enrollApi = (news_ids: number[]) => request<null>({ url: "/risk/packages", method: "post", data: { news_ids } })

export const getVocabularyApi = () => request<Research.Vocabulary>({ url: "/risk/tag-vocabulary", method: "get" })
export const saveVocabularyApi = (data: { base_revision: number, entries: Research.VocabularyEntry[] }) => request<Research.Vocabulary>({ url: "/risk/tag-vocabulary", method: "put", data })
export const getEventVersionApi = (event: number, version: number) => request<Research.VersionDetail>({ url: `/risk/events/${event}/versions/${version}`, method: "get" })
