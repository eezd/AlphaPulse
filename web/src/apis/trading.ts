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

export interface PlanCondition {
  direction: "above" | "below"
  price: string
  timeframe: "mark" | "1m" | "15m" | "1h" | "4h"
  basis: string
}

/** submit_trade_plan 的参数原样保存；数值为字符串 */
export interface TradeDecision {
  action: "plan" | "keep" | "exit" | "wait"
  summary: string
  reasons: string
  risks: string
  reevaluate: PlanCondition[]
  direction: "long" | "short" | null
  confidence: "high" | "medium" | "low" | null
  entries: { price: string, ratio: string, basis: string }[] | null
  stop: string | null
  stop_basis: string | null
  take_profits: { price: string, basis: string }[] | null
  entry_valid_hours: string | null
  max_hold_hours: string | null
  invalidation: PlanCondition | null
}

/** 程序按交易资金算出的数量和风险 */
export interface Sizing {
  direction: "long" | "short"
  entries: { price: string, ratio: string, quantity: string, notional: string }[]
  stop: string
  take_profits: { price: string, ratio: string, quantity: string }[]
  average_price: string
  quantity: string
  notional: string
  capital: string
  leverage: string
  risk_amount: string
  risk_at_stop: string
  risk_pct: string
  capped_by_leverage: boolean
  reward_risk: string
  reward_risk_first_only: string
}

export interface Analysis {
  id: number
  symbol: string
  angle: Angle
  reasons: string[]
  dry_run: boolean
  status: "running" | "done" | "failed"
  action: TradeDecision["action"] | null
  decision: TradeDecision | null
  sizing: Sizing | null
  error: string | null
  model: string | null
  reasoning_effort: string | null
  usage: { input_tokens: number, output_tokens: number } | null
  started_at: string
  finished_at: string | null
  plan_status: "pending" | "open" | "closed" | "canceled" | "replaced" | null
  valid_until: string | null
  first_fill_at: string | null
  close_deadline: string | null
  closed_at: string | null
  close_reason: string | null
  result: Record<string, unknown> | null
}

/** 与协议无关的消息格式，同 Chat */
export type TranscriptMessage
  = | { role: "user", text: string }
    | { role: "assistant", text: string, tool_calls: { id: string, name: string, arguments: Record<string, unknown> }[] }
    | { role: "tool", tool_call_id: string, name: string, result: string }

export interface AnalysisDetail extends Analysis {
  text: string | null
  transcript: TranscriptMessage[] | null
}

export function requestAnalysis(symbol: string): Promise<void> {
  return http.post(`/trading/symbols/${symbol}/analyze`)
}

export function listAnalyses(params: { symbol?: string, limit?: number }): Promise<Analysis[]> {
  return http.get("/trading/analyses", { params })
}

export function getAnalysis(id: number): Promise<AnalysisDetail> {
  return http.get(`/trading/analyses/${id}`)
}
