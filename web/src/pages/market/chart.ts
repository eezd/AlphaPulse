import type { DeepPartial, Period, Styles } from "klinecharts"

export const UP = "#0ecb81"
export const DOWN = "#f6465d"
const GRID = "#2b3139"
const TEXT = "#848e9c"

/** “分时”用 1m 数据画面积图，与币安一致 */
export const TIME_LINE = "time"

export const INTERVAL_LABELS: Record<string, string> = {
  [TIME_LINE]: "分时",
  "1s": "1秒",
  "1m": "1分",
  "3m": "3分",
  "5m": "5分",
  "15m": "15分",
  "30m": "30分",
  "1h": "1小时",
  "2h": "2小时",
  "4h": "4小时",
  "6h": "6小时",
  "8h": "8小时",
  "12h": "12小时",
  "1d": "1日",
  "3d": "3日",
  "1w": "1周",
  "1M": "1月"
}

export const DEFAULT_FAVORITE_INTERVALS = [TIME_LINE, "15m", "1h", "4h", "1d", "1w"]

export const MAIN_INDICATORS = ["MA", "EMA", "BOLL", "SAR"]
export const SUB_INDICATORS = ["VOL", "MACD", "KDJ", "RSI", "OBV", "WR", "CCI", "DMI", "TRIX"]

export const DRAW_TOOLS = [
  { name: "horizontalStraightLine", label: "水平线" },
  { name: "horizontalRayLine", label: "水平射线" },
  { name: "verticalStraightLine", label: "垂直线" },
  { name: "straightLine", label: "直线" },
  { name: "rayLine", label: "射线" },
  { name: "segment", label: "线段" },
  { name: "priceLine", label: "价格线" },
  { name: "parallelStraightLine", label: "平行线" },
  { name: "priceChannelLine", label: "价格通道" },
  { name: "fibonacciLine", label: "斐波那契" },
  { name: "simpleAnnotation", label: "标注" }
]

/** 后端周期字符串 → KLineChart 周期 */
export function toPeriod(interval: string): Period {
  if (interval === TIME_LINE)
    return { type: "minute", span: 1 }
  const span = Number.parseInt(interval)
  const unit = interval.slice(String(span).length)
  const type = ({ s: "second", m: "minute", h: "hour", d: "day", w: "week", M: "month" } as const)[unit]
  if (!type)
    throw new Error(`未知周期：${interval}`)
  return { type, span }
}

/** 在内置 dark 主题上覆盖币安配色 */
export const BINANCE_STYLES: DeepPartial<Styles> = {
  grid: {
    horizontal: { color: GRID },
    vertical: { color: GRID }
  },
  candle: {
    bar: {
      upColor: UP,
      downColor: DOWN,
      noChangeColor: TEXT,
      upBorderColor: UP,
      downBorderColor: DOWN,
      noChangeBorderColor: TEXT,
      upWickColor: UP,
      downWickColor: DOWN,
      noChangeWickColor: TEXT
    },
    priceMark: {
      last: { upColor: UP, downColor: DOWN, noChangeColor: TEXT }
    }
  },
  indicator: {
    ohlc: { upColor: UP, downColor: DOWN, noChangeColor: TEXT }
  },
  xAxis: { axisLine: { color: GRID }, tickText: { color: TEXT } },
  yAxis: { axisLine: { color: GRID }, tickText: { color: TEXT } },
  separator: { color: GRID }
}
