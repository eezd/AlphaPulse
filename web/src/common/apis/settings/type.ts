export type Protocol = "openai" | "anthropic"

export type Effort = "low" | "medium" | "high" | "xhigh" | "max"

export interface Provider {
  id: number
  name: string
  protocol: Protocol
  base_url: string
  api_key_hint: string
  model: string
  max_tokens: number
  context_window: number
  /** 为空表示不传，由模型决定 */
  reasoning_effort: Effort | null
  is_default: boolean
  updated_at: string
}

export type ListProvidersResponseData = Provider[]

export interface ProviderForm {
  name: string
  protocol: Protocol
  base_url: string
  /** 编辑时为空表示不修改 */
  api_key: string
  model: string
  max_tokens: number
  context_window: number
  /** 为空表示不传，由模型决定 */
  reasoning_effort: Effort | null
  /** 修改时不能直接取消当前默认模型；通过将其他模型设为默认切换。 */
  is_default: boolean
}

export interface TestResult {
  ok: boolean
  elapsed_ms: number
  reply: string | null
  error: string | null
}

export type CreateProviderResponseData = Provider

export type PatchProviderResponseData = Provider

export type DeleteProviderResponseData = null

export type TestProviderResponseData = TestResult

export type ExchangeName = "binance" | "bybit"

export interface Exchange {
  exchange: ExchangeName
  configured_exchanges: ExchangeName[]
  configured: boolean
  api_key_hint: string | null
  /** 是否有币种在交易：交易中不能修改 key */
  running: boolean
  updated_at: string | null
}

export interface ExchangeTestResult {
  ok: boolean
  elapsed_ms: number
  available_usdt: string | null
  wallet_usdt: string | null
  /** 双向持仓模式；引擎使用单向持仓，启动时会切换 */
  dual_side_position: boolean | null
  error: string | null
}

export type GetExchangeResponseData = Exchange

export type PutExchangeResponseData = Exchange

export interface PutExchangeBodyRequestData { exchange?: ExchangeName, api_key?: string, api_secret?: string }

export type DeleteExchangeResponseData = Exchange

export type TestExchangeResponseData = ExchangeTestResult
