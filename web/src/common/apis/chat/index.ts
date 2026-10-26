import type * as Chat from "./type"
import { request } from "@/http/axios"
import { consumeStream } from "@/http/stream"

/** 读取当前对话及生成状态 */
export function getChatSessionApi() {
  return request<Chat.GetChatSessionResponseData>({
    url: "/chat/session",
    method: "get"
  })
}

/** 重置当前对话，可按选择清空上下文 */
export function resetChatApi(clearContext: boolean) {
  return request<Chat.ResetChatResponseData>({
    url: "/chat/reset",
    method: "post",
    data: { clear_context: clearContext }
  })
}

/** 读取 Chat 上下文 */
export function getChatContextApi() {
  return request<Chat.GetChatContextResponseData>({
    url: "/chat/context",
    method: "get"
  })
}

/** 保存 Chat 上下文版本 */
export function saveChatContextApi(content: string) {
  return request<Chat.SaveChatContextResponseData>({
    url: "/chat/context",
    method: "put",
    data: { content }
  })
}

/** 读取 Chat 上下文历史 */
export function listChatContextVersionsApi() {
  return request<Chat.ListChatContextVersionsResponseData>({
    url: "/chat/context/versions",
    method: "get"
  })
}

/** POST 只发送一次；这里声明 SSE 事件协议，生命周期由 Chat 管理。 */
export function sendChatApi(body: Chat.SendChatRequestData, signal: AbortSignal, version: number, onEvent: Chat.ChatStreamHandler) {
  return consumeStream<Chat.ChatStreamEvent["data"]>("/api/chat/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal
  }, version, () => {}, (name, data) => {
    // SSE 将事件名与 JSON 分开发送；在协议边界恢复同一事件的关联类型。
    const event = { name, data } as Chat.ChatStreamEvent
    onEvent(event)
  })
}
