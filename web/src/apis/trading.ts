import type { Effort } from "@/apis/settings"
import { http } from "@/http"

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
  added_at: string
  analysis_requested_at: string | null
  analyzing: boolean
  /** 执行器正在管理的计划 */
  plan_id: number | null
  plan_status: "pending" | "open" | "closing" | null
  close_deadline: string | null
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

export function addSymbol(symbol: string): Promise<void> {
  return http.post("/trading/symbols", { symbol })
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
  timeframe: "mark" | "1m" | "5m" | "15m" | "1h" | "4h"
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
  /** 区间与挂单价；更早格式的分析没有这个字段 */
  setup?: { zone_low: string, zone_high: string, entry_price?: string, basis: string } | null
  valid_hours?: string | null
}

/** 程序按止损和交易资金算出的数量与风险 */
export interface EntrySizing {
  price: string
  stop: string
  stop_pct: string
  quantity: string
  notional: string
  leverage: string
  risk_amount: string
  risk_at_stop: string
  risk_pct: string
  capped_by_leverage: boolean
}

export interface Sizing {
  direction: "long" | "short"
  setup: { zone_low: string, zone_high: string }
  /** 限价、止损和数量 */
  entry: EntrySizing
  atr: string
  position_qty: string
}

export interface Analysis {
  id: number
  symbol: string
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
  plan_status: PlanStatus | null
  valid_until: string | null
  first_fill_at: string | null
  close_deadline: string | null
  closed_at: string | null
  close_reason: string | null
  result: { realized_pnl: string, commission: string, net: string } | null
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

export type PlanStatus = "proposed" | "pending" | "open" | "closing" | "closed" | "canceled" | "replaced" | "stale" | "applied"

export const PLAN_STATUS_LABELS: Record<PlanStatus, string> = {
  proposed: "待采纳",
  pending: "挂单中",
  open: "持仓中",
  closing: "平仓中",
  closed: "已结束",
  canceled: "已撤销",
  replaced: "已被替换",
  stale: "已作废",
  applied: "已执行"
}

/** 执行器的订单流水 */
export interface OrderRow {
  id: number
  purpose: "entry" | "stop" | "tp" | "close"
  leg: number | null
  side: "BUY" | "SELL"
  type: string
  price: string | null
  trigger_price: string | null
  quantity: string
  status: string
  filled_qty: string
  avg_price: string | null
  error: string | null
  created_at: string
}

export const CLOSE_REASON_LABELS: Record<string, string> = {
  stop: "止损",
  take_profit: "止盈",
  time: "到达持仓时限",
  exit: "模型决定结束",
  external: "外部平仓",
  stop_failed: "止损挂单失败",
  close: "市价平仓",
  // 计划 3.1 之前的旧原因，历史仓位里还会出现
  invalidation: "短线失效条件触发",
  replaced: "被新计划替换",
  expired: "限价单到期未成交",
  stopped: "已停止交易",
  entry_failed: "入场单被拒"
}

/** 止盈阶梯（与后端 plan.TAKE_PROFIT_STEPS 一致）：[距成交均价的涨跌幅, 平仓比例] */
export const TAKE_PROFIT_STEPS: Record<"long" | "short", [number, number][]> = {
  long: [[0.005, 0.6], [0.0075, 0.3], [0.01, 0.1]],
  short: [[0.005, 0.8], [0.0075, 0.2]]
}

/** 已平仓的仓位：一个计划从首次入场到持仓归零；数值为十进制字符串 */
export interface PositionHistory {
  analysis_id: number
  symbol: string
  direction: "long" | "short"
  opened_at: string
  closed_at: string
  close_reason: string
  /** 计划被替换后接管持仓的新计划没有自己的入场成交，为空 */
  quantity: string | null
  entry_price: string | null
  /** 外部平仓没有成交记录，为空 */
  exit_price: string | null
  realized_pnl: string
  commission: string
  net: string
}

export function listPositionHistory(params: { before?: number, limit?: number }): Promise<PositionHistory[]> {
  return http.get("/trading/positions/history", { params })
}

export function getSymbolPlan(symbol: string): Promise<{ plan: Analysis | null, orders: OrderRow[] }> {
  return http.get(`/trading/symbols/${symbol}/plan`)
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
