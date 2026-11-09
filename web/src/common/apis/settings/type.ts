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
export type ListProviderModelsResponseData = string[]

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
