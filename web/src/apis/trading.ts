import type { Effort } from "@/apis/settings"
import { http } from "@/http"

export type Angle = "short" | "long"

export const ANGLE_LABELS: Record<Angle, string> = { short: "短线（24h 内平仓）", long: "长线（12–48h 平仓）" }

/** 以下数值字段都是币安返回的十进制字符串，避免浮点误差 */
export interface SnapshotPosition {
  symbol: string
  side: "LONG" | "SHORT"
  amount: string
  entry_price: string
  break_even_price: string
  mark_price: string
  unrealized_pnl: string
  liquidation_price: string
  notional: string
  margin: string
  leverage: number
  margin_type: string
}

export interface SnapshotOrder {
  symbol: string
  client_id: string
  side: "BUY" | "SELL"
  type: string
  price: string
  quantity: string
  filled: string
  reduce_only: boolean
  time: number
}

export interface SnapshotAlgoOrder {
  symbol: string
  client_id: string
  side: "BUY" | "SELL"
  type: string
  trigger_price: string
  quantity: string
  close_position: boolean
  reduce_only: boolean
  time: number
}

export interface Snapshot {
  updated_at: string
  /** 最近一次成功同步币安的时间 */
  synced_at: string | null
  error: string | null
  account: { wallet_balance: string, margin_balance: string, available_balance: string, unrealized_pnl: string } | null
  positions: SnapshotPosition[]
  orders: SnapshotOrder[]
  algo_orders: SnapshotAlgoOrder[]
}

export interface TradingStatus {
  configured: boolean
  running: boolean
  running_changed_at: string | null
  /** 设定的交易资金，十进制字符串；未设置时不能开始交易 */
  capital_usdt: string | null
  /** 实际用于仓位计算的资金：min(交易资金, 账户权益) */
  effective_capital: string | null
  provider_id: number | null
  reasoning_effort: Effort | null
  /** trader 进程离线时为空 */
  snapshot: Snapshot | null
  today: { analyses: number, limit: number, input_tokens: number, output_tokens: number }
}

export interface TradingSymbol {
  symbol: string
  angle: Angle
  added_at: string
  analysis_requested_at: string | null
  analyzing: boolean
  plan_status: "pending" | "open" | null
}

export interface SymbolOption {
  symbol: string
  base: string
}

export interface TradingEvent {
  id: number
  created_at: string
  symbol: string | null
  analysis_id: number | null
  kind: string
  level: "info" | "warn" | "error"
  message: string
  data: Record<string, unknown> | null
}

/** 本程序下的单；其他是用户手动下的 */
export function isOwnOrder(clientId: string) {
  return /^ap\d+$/.test(clientId)
}

export function getStatus(): Promise<TradingStatus> {
  return http.get("/trading/status")
}

export function startTrading(): Promise<TradingStatus> {
  return http.post("/trading/start")
}

export function stopTrading(): Promise<TradingStatus> {
  return http.post("/trading/stop")
}

export function patchConfig(body: { capital_usdt?: string | null, provider_id?: number | null, reasoning_effort?: Effort | null }): Promise<TradingStatus> {
  return http.patch("/trading/config", body)
}

export function listSymbolOptions(): Promise<SymbolOption[]> {
  return http.get("/trading/symbol-options")
}

export function listSymbols(): Promise<TradingSymbol[]> {
  return http.get("/trading/symbols")
}

export function addSymbol(symbol: string, angle: Angle): Promise<void> {
  return http.post("/trading/symbols", { symbol, angle })
}

export function patchSymbol(symbol: string, angle: Angle): Promise<void> {
  return http.patch(`/trading/symbols/${symbol}`, { angle })
}

export function deleteSymbol(symbol: string): Promise<void> {
  return http.delete(`/trading/symbols/${symbol}`)
}

export function listEvents(params: { symbol?: string, before?: number, limit?: number }): Promise<TradingEvent[]> {
  return http.get("/trading/events", { params })
}
