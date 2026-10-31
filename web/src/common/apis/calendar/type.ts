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
  unit: string | null
  importance: number
  scheduled_at: string
  status: "scheduled" | "waiting" | "delayed" | "released" | "withdrawn" | "error" | "historical" | "unavailable" | "recorded"
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
  kind: "data" | "event" | "holiday"
  calendar_date: string
  time_precision: "minute" | "day" | "tentative"
  period_label: string | null
  importance_source: "project" | "jin10"
  previous_original: string | null
  previous_revised: string | null
  forecast_before_release: boolean
  forecast_observed_at: string | null
  field_sources: Record<string, { source: string, observed_at: string | null, url: string }>
  related_release_id: number | null
  related_revision_no: number | null
}

export interface CalendarData {
  items: CalendarItem[]
  countries: { code: string, name: string }[]
  worker: { at: string, sources: Record<string, { source: string, checked_at: string, error: string | null }> } | null
  server_time: string
  jin10: { at: string, state: string, error: string | null, history_next?: string | null, last_success_at: string | null } | null
  coverage: { date: string, captured_at: string, counts: Record<"data" | "event" | "holiday", number> }[]
}

export interface CalendarRevision {
  source: string
  revision_no: number
  captured_at: string
  payload: { title: string, scheduled_at: string, schedule_status: string, values: Record<string, { actual?: string | null, previous?: string | null, forecast?: string | null, previous_revised?: string | null, precision?: string, description?: string | null, indicator?: string }> }
}

export interface CalendarHistory {
  period_label?: string | null
  forecast_before_release?: boolean
  forecast_observed_at?: string | null
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
