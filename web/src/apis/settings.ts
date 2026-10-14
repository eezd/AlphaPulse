import { http } from "@/http"

export type Protocol = "openai" | "anthropic"

/** 思考强度，与后端 llm.Effort 一致 */
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const

export type Effort = typeof EFFORTS[number]

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
  is_default: boolean
}

export interface TestResult {
  ok: boolean
  elapsed_ms: number
  reply: string | null
  error: string | null
}

export function listProviders(): Promise<Provider[]> {
  return http.get("/settings/providers")
}

export function createProvider(body: ProviderForm): Promise<Provider> {
  return http.post("/settings/providers", body)
}

export function patchProvider(id: number, body: Partial<ProviderForm>): Promise<Provider> {
  return http.patch(`/settings/providers/${id}`, body)
}

export function deleteProvider(id: number): Promise<void> {
  return http.delete(`/settings/providers/${id}`)
}

export function testProvider(id: number): Promise<TestResult> {
  // 模型接口读超时 600s 且 SDK 会重试 2 次，前端放宽等待时间
  return http.post(`/settings/providers/${id}/test`, null, { timeout: 1900000 })
}
