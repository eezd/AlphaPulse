import type * as Calendar from "./type"
import { request } from "@/http/axios"

/** 按日期范围查询经济日历 */
export function listCalendarApi(start: string, days: number) {
  return request<Calendar.ListCalendarResponseData>({
    url: "/calendar",
    method: "get",
    params: { start, days }
  })
}

/** 读取本期事件修订记录 */
export function calendarRevisionsApi(id: number) {
  return request<Calendar.CalendarRevisionsResponseData>({
    url: `/calendar/${id}/revisions`,
    method: "get"
  })
}

/** 读取同指标历次公布值 */
export function calendarHistoryApi(id: number, indicator: string) {
  return request<Calendar.CalendarHistoryResponseData>({
    url: `/calendar/${id}/history`,
    method: "get",
    params: { indicator }
  })
}

/** 读取当前官方历史序列 */
export function calendarSeriesApi(id: number, indicator: string) {
  return request<Calendar.CalendarSeriesResponseData>({
    url: `/calendar/${id}/series`,
    method: "get",
    params: { indicator }
  })
}
