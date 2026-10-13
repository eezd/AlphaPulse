import { http } from "@/http"

export type Market = "spot" | "um"

export interface IntervalProgress {
  done: boolean
  percent: number
}

export interface MarketSymbol {
  id: number
  market: Market
  symbol: string
  base: string
  quote: string
  price_precision: number
  volume_precision: number
  status: "pending" | "backfilling" | "ready" | "error" | "delisted"
  is_default: boolean
  added_at: string
  last_error: string | null
  progress: Record<string, IntervalProgress>
}

export interface TradableSymbol {
  symbol: string
  base: string
  quote: string
}

/** 币安 24hr ticker 原始字段（只列用到的） */
export interface Ticker24h {
  lastPrice: string
  priceChangePercent: string
  highPrice: string
  lowPrice: string
  volume: string
  quoteVolume: string
}

/** [open_time, open, high, low, close, volume, quote_volume] */
export type KlineRow = [number, number, number, number, number, number, number]

export function listSymbols(): Promise<MarketSymbol[]> {
  return http.get("/market/symbols")
}

export function addSymbol(market: Market, symbol: string): Promise<void> {
  return http.post("/market/symbols", { market, symbol })
}

export function deleteSymbol(id: number): Promise<void> {
  return http.delete(`/market/symbols/${id}`)
}

export function listTradable(market: Market): Promise<TradableSymbol[]> {
  return http.get("/market/exchange-info", { params: { market } })
}

export function listKlines(params: { market: Market, symbol: string, interval: string, end?: number, limit?: number }): Promise<KlineRow[]> {
  return http.get("/market/klines", { params })
}

export function getTicker(market: Market, symbol: string): Promise<Ticker24h> {
  return http.get("/market/ticker", { params: { market, symbol } })
}
