import type { CalendarItem } from "@@/apis/calendar/type"
import dayjs from "dayjs"
import timezone from "dayjs/plugin/timezone"
import utc from "dayjs/plugin/utc"

dayjs.extend(utc)
dayjs.extend(timezone)
export const zone = "Asia/Shanghai"
export const sources: Record<string, string> = { bls: "BLS", bea: "BEA", fed: "美联储", umich: "密歇根大学", ecb: "欧洲央行", eurostat: "Eurostat", boe: "英国央行", ons: "ONS", destatis: "Destatis", insee: "INSEE", dol: "美国劳工部 DOL", eia: "美国能源信息署 EIA", adp: "ADP", jin10: "金十" }
export function flagUrl(item: CalendarItem) {
  if (["US", "EA", "GB", "DE", "FR"].includes(item.country_code)) return `/flags/${item.country_code.toLowerCase()}.svg`
  return /^[A-Z]{2}$/.test(item.country_code) ? `https://cdn.jin10.com/assets/img/commons/flag/${encodeURIComponent(item.country_name)}.png` : null
}
export function timeLabel(item: CalendarItem) {
  return item.time_precision === "day" ? "全天" : item.time_precision === "tentative" ? "待定" : dayjs(item.scheduled_at).tz(zone).format("HH:mm")
}
export const value = (v: string | null, unit: string | null) => v === null ? "—" : `${v}${unit ?? ""}`
export function forecastNote(item: Pick<CalendarItem, "forecast_observed_at" | "scheduled_at">) {
  return item.forecast_observed_at && Date.parse(item.forecast_observed_at) >= Date.parse(item.scheduled_at) ? "发布后补取" : "时点未核实"
}
