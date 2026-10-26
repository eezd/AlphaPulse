import { ElMessage } from "element-plus"
import { ApiError } from "@/http/axios"
import { useUserStore } from "@/pinia/stores/user"
import { streamCanceled } from "./stream"

/** Bearer SSE：只有只读订阅重连；身份失效和权限拒绝均停止。 */
export function useEventSource<T>(subscribe: (signal: AbortSignal, version: number, onOpen: () => void, onMessage: (data: T) => void) => Promise<void>, onMessage: (data: T) => void) {
  const connected = ref(false)
  const user = useUserStore()
  const version = user.sessionVersion
  let closed = false
  let retryMs = 1000
  let controller: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let reported = false
  async function connect() {
    if (closed || version !== user.sessionVersion)
      return
    controller = new AbortController()
    try {
      await subscribe(controller.signal, version, () => {
        connected.value = true
        retryMs = 1000
        reported = false
      }, onMessage)
    } catch (error) {
      if (streamCanceled(error)) return
      if (error instanceof ApiError && [401, 403].includes(error.code)) {
        closed = true
        if (error.code === 403) ElMessage.error(error.message)
        return
      }
      if (!reported) {
        ElMessage.error(error instanceof Error ? error.message : "实时连接中断")
        reported = true
      }
    } finally {
      connected.value = false
    }
    if (!closed && version === user.sessionVersion) {
      timer = setTimeout(connect, retryMs)
      retryMs = Math.min(retryMs * 2, 30000)
    }
  }
  function close() {
    closed = true
    connected.value = false
    clearTimeout(timer)
    controller?.abort()
  }
  watch(() => user.sessionVersion, close, { flush: "sync" })
  void connect()
  onBeforeUnmount(close)
  return { connected }
}
