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

export type ListSymbolsResponseData = MarketSymbol[]

export type AddSymbolResponseData = null

export type DeleteSymbolResponseData = null

export type ListTradableResponseData = TradableSymbol[]

export type ListKlinesResponseData = KlineRow[]

export interface ListKlinesParamsRequestData { market: Market, symbol: string, interval: string, end?: number, limit?: number }

export type GetTickerResponseData = Ticker24h

export interface KlineMessage {
  t: number
  o: string
  h: string
  l: string
  c: string
  v: string
  q: string
  x: boolean
}

export interface TickerMessage {
  c: string
  o: string
  h: string
  l: string
  v: string
  q: string
  E: number
}

export interface Subscription {
  quotes?: boolean
  market: Market
  symbol: string
  interval: string
}

export interface QuoteMessage { type: "depth" | "trade" | "mark_price", market: Market, symbol: string, data: { bids?: string[][], asks?: string[][], at?: number, id?: number, price?: string, quantity?: string, time?: number, buyer_maker?: boolean, mark_price?: string, index_price?: string, funding_rate?: string, next_funding_at?: number } }

export type MarketMessage
  = QuoteMessage | { type: "kline", market: Market, symbol: string, interval: string, data: KlineMessage }
    | { type: "ticker", market: Market, symbol: string, interval: null, data: TickerMessage }
    | { type: "error", message: string }
