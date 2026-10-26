import axios, { CanceledError } from "axios"
import { ApiError, readEnvelope, rejectAuthentication } from "@/http/axios"
import { useUserStore } from "@/pinia/stores/user"
import { parseEvents } from "./sse"

/** 只执行一次请求；重连只由只读订阅的调用方负责。 */
export async function consumeStream<T>(url: string, init: RequestInit, version: number, onOpen: () => void, onEvent: (event: string, data: T) => void) {
  const user = useUserStore()
  const assertCurrent = () => {
    if (version !== user.sessionVersion || !user.token || init.signal?.aborted)
      throw new CanceledError("会话或订阅已结束")
  }
  assertCurrent()
  const response = await fetch(url, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${user.token}` }
  })
  assertCurrent()
  if (!response.ok) {
    const body = readEnvelope(await response.json())
    assertCurrent()
    if (response.status === 401)
      rejectAuthentication(version)
    throw new ApiError(response.status, body.message || `请求失败：HTTP ${response.status}`)
  }
  if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream"))
    throw new ApiError(502, "服务器未返回事件流")
  onOpen()
  for await (const event of parseEvents(response.body)) {
    assertCurrent()
    const data = JSON.parse(event.data)
    if (event.event === "auth_error") {
      rejectAuthentication(version)
      throw new ApiError(401, data.message)
    }
    if (event.event === "error" && typeof data.code === "number")
      throw new ApiError(data.code, data.message)
    onEvent(event.event, data)
  }
  assertCurrent()
}

export function streamCanceled(error: unknown): boolean {
  return axios.isCancel(error) || (error instanceof Error && error.name === "AbortError")
}
