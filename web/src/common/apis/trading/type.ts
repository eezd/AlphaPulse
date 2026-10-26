import type { Effort } from "../settings/type"

/** 以下数值字段都是交易所返回的十进制字符串，避免浮点误差 */
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
  exchange: "binance" | "bybit"
  updated_at: string
  /** 最近一次成功同步交易所的时间 */
  synced_at: string | null
  error: string | null
  account: { wallet_balance: string, margin_balance: string, available_balance: string, unrealized_pnl: string } | null
  positions: SnapshotPosition[]
  orders: SnapshotOrder[]
  algo_orders: SnapshotAlgoOrder[]
}

export interface TradingStatus {
  exchange: "binance" | "bybit"
  configured: boolean
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
  /** 该币是否在交易；停止时分析为试运行，不挂新的入场单 */
  running: boolean
  running_changed_at: string | null
  /** 该币的交易资金，十进制字符串；未设置时不能开始交易 */
  capital_usdt: string | null
  /** 实际用于仓位计算的资金：min(交易资金, 账户权益) */
  effective_capital: string | null
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

export type GetStatusResponseData = TradingStatus

export type StartTradingResponseData = null

export type StopTradingResponseData = null

export type PatchSymbolResponseData = null

export interface PatchSymbolBodyRequestData { capital_usdt: string | null }

export type PatchConfigResponseData = TradingStatus

export interface PatchConfigBodyRequestData { provider_id?: number | null, reasoning_effort?: Effort | null, daily_analysis_limit?: number }

export type ListSymbolOptionsResponseData = SymbolOption[]

export type ListSymbolsResponseData = TradingSymbol[]

export type AddSymbolResponseData = null

export type DeleteSymbolResponseData = null

export type ListEventsResponseData = TradingEvent[]

export interface ListEventsParamsRequestData { symbol?: string, before?: number, limit?: number }

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

/** 由当前交易所成交记录还原的已平仓仓位（含程序外的手动仓位）；数值为十进制字符串 */
export interface PositionHistory {
  symbol: string
  direction: "long" | "short"
  opened_at: string
  closed_at: string
  quantity: string
  entry_price: string
  exit_price: string
  realized_pnl: string
  commission: string
  net: string
  source: "program" | "manual"
  /** 手动仓位没有关联计划，为空 */
  analysis_id: number | null
  close_reason: string | null
}

export interface PositionHistoryQuery {
  symbol?: string
  /** 按平仓时间筛选，ISO 时间 */
  start?: string
  end?: string
  outcome?: "profit" | "loss"
  source?: "program" | "manual"
  page: number
  limit: number
}

export interface PositionHistoryPage {
  /** 按筛选条件统计全部仓位 */
  summary: { count: number, wins: number, net: string, commission: string }
  total: number
  items: PositionHistory[]
}

export type ListPositionHistoryResponseData = PositionHistoryPage

export type ListPositionHistorySymbolsResponseData = string[]

export type SignalHorizon = "1h" | "4h"

/** 新闻信号前向记录：按模型标注方向（利多做多、利空做空）交易的统计；收益单位 bp */
export interface NewsSignalStat {
  category: "price_report" | "event" | "irrelevant"
  symbol: string
  horizon: SignalHorizon
  n: number
  mean_bp: number | null
  t: number | null
  /** 扣除来回手续费后的平均收益 */
  net_bp: number | null
  win_rate: number | null
}

export interface NewsSignalItem {
  news_id: number
  published_at: string
  source_code: string
  title: string
  category: NewsSignalStat["category"]
  direction: "bullish" | "bearish" | "neutral"
  /** 进场后的原始涨跌（bp，未按方向取正负）；发布未满 4 小时为空 */
  returns: Record<string, Record<SignalHorizon, number>> | null
}

export interface NewsSignals {
  /** 前向记录的起点：之后发布的新闻才计入 */
  start: string
  labeled: number
  filled: number
  stats: NewsSignalStat[]
  recent: NewsSignalItem[]
}

export type GetNewsSignalsResponseData = NewsSignals

export interface GetSymbolPlanResponseData { plan: Analysis | null, orders: OrderRow[] }

export type RequestAnalysisResponseData = null

export type ManualActionResponseData = null

export type ListAnalysesResponseData = Analysis[]

export interface ListAnalysesParamsRequestData { symbol?: string, limit?: number }

export type GetAnalysisResponseData = AnalysisDetail
