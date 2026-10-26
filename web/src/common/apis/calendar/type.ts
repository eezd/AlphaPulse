export interface CalendarItem {
  release_id: number
  indicator: string
  name: string
  country_code: string
  country_name: string
  event_name: string
  title: string
  period: string | null
  stage: string | null
  source: string
  unit: string
  importance: number
  scheduled_at: string
  status: "scheduled" | "waiting" | "delayed" | "released" | "withdrawn" | "error" | "historical" | "unavailable"
  actual: string | null
  previous: string | null
  forecast: string | null
  previous_basis: string | null
  impact: "pending" | "bullish" | "bearish" | "neutral" | "uncertain"
  impact_basis: "forecast" | "previous" | null
  explanation: string
  release_url: string | null
  schedule_url: string
  first_received_at: string | null
  last_checked_at: string | null
  error: string | null
  revision_no: number
}

export interface CalendarData {
  items: CalendarItem[]
  countries: { code: string, name: string }[]
  worker: { at: string, sources: Record<string, { source: string, checked_at: string, error: string | null }> } | null
  server_time: string
}

export interface CalendarRevision {
  revision_no: number
  captured_at: string
  payload: { title: string, scheduled_at: string, schedule_status: string, values: Record<string, { actual: string | null, previous: string | null }> }
}

export interface CalendarHistory {
  release_id: number
  scheduled_at: string
  title: string
  period: string | null
  stage: string | null
  actual: string
  previous: string | null
  forecast: string | null
  first_recorded_actual: string | null
  first_received_at: string
  release_url: string | null
  revised: boolean
}

export type ListCalendarResponseData = CalendarData

export type CalendarRevisionsResponseData = CalendarRevision[]

export type CalendarHistoryResponseData = CalendarHistory[]

export interface CalendarSeriesValue {
  period: string
  stage: string
  value: string
  captured_at: string
  source_url: string
}

export type CalendarSeriesResponseData = CalendarSeriesValue[]
