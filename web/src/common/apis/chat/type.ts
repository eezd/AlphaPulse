import type { Effort } from "../settings/type"

export interface ToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

interface MessageMetadata {
  id: number
  provider_id: number | null
  usage: { input_tokens?: number, output_tokens?: number } | null
  created_at: string
}

export type ChatMessage = MessageMetadata & (
  | { role: "user", content: { text: string } }
  | { role: "assistant", content: { text: string, tool_calls: ToolCall[], stopped?: boolean, error?: string, thinking_blocks?: unknown[] } }
  | { role: "tool", content: { tool_call_id: string, name: string, result: string } }
)

export interface ChatSession {
  id: number
  created_at: string
  messages: ChatMessage[]
  estimated_tokens: number
  generating: boolean
}

export interface ChatContext {
  id: number | null
  content: string
  source: "user" | "chat" | "reset" | null
  created_at: string | null
}

export type GetChatSessionResponseData = ChatSession

export type ResetChatResponseData = null

export type GetChatContextResponseData = ChatContext

export type SaveChatContextResponseData = ChatContext

export type ListChatContextVersionsResponseData = ChatContext[]

export interface SendChatRequestData {
  text: string
  provider_id: number
  reasoning_effort: Effort | null
}

export type ChatStreamEvent
  = | { name: "prompt", data: { estimated_tokens: number, context_window: number, provider: string } }
    | { name: "thinking", data: Record<string, never> }
    | { name: "done", data: Record<string, never> }
    | { name: "context_updated", data: { id: number } }
    | { name: "delta", data: { text: string } }
    | { name: "tool_call", data: ToolCall }
    | { name: "tool_result", data: { id: string, name: string, preview: string } }
    | { name: "skill_draft", data: { skill_id: number, path: string, version: number } }
    | { name: "usage", data: { input_tokens: number, output_tokens: number } }
    | { name: "error", data: { message: string } }

export type ChatStreamHandler = (event: ChatStreamEvent) => void
