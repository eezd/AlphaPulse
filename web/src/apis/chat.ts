import { http } from "@/http"

export interface ToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface ChatMessage {
  id: number
  role: "user" | "assistant" | "tool"
  /** user: {text}；assistant: {text, tool_calls, stopped?, error?}；tool: {tool_call_id, name, result} */
  content: Record<string, any>
  provider_id: number | null
  usage: { input_tokens?: number, output_tokens?: number } | null
  created_at: string
}

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

export function getChatSession(): Promise<ChatSession> {
  return http.get("/chat/session")
}

export function resetChat(clearContext: boolean): Promise<void> {
  return http.post("/chat/reset", { clear_context: clearContext })
}

export function getChatContext(): Promise<ChatContext> {
  return http.get("/chat/context")
}

export function saveChatContext(content: string): Promise<ChatContext> {
  return http.put("/chat/context", { content })
}

export function listChatContextVersions(): Promise<ChatContext[]> {
  return http.get("/chat/context/versions")
}
