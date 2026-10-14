import { router } from "@/router"

/** POST 方式的 SSE（EventSource 只支持 GET）：逐个事件回调；stop() 断开连接，服务端随之停止生成 */
export function useChatStream() {
  const streaming = ref(false)
  let controller: AbortController | null = null

  async function send(body: unknown, onEvent: (name: string, data: any) => void) {
    controller = new AbortController()
    streaming.value = true
    try {
      const response = await fetch("/api/chat/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal
      })
      if (response.status === 401) {
        router.replace({ name: "login", query: { redirect: router.currentRoute.value.fullPath } })
        return
      }
      if (!response.ok || !response.body) {
        const detail = await response.json().catch(() => null)
        onEvent("error", { message: detail?.detail ?? `请求失败：HTTP ${response.status}` })
        return
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
      let buffer = ""
      while (true) {
        const { value, done } = await reader.read()
        if (done)
          break
        buffer += value
        // 事件之间以空行分隔；保留最后一段不完整的
        const blocks = buffer.split("\n\n")
        buffer = blocks.pop() ?? ""
        for (const block of blocks) {
          let name = "message"
          let data = ""
          for (const line of block.split("\n")) {
            if (line.startsWith("event:"))
              name = line.slice(6).trim()
            else if (line.startsWith("data:"))
              data += line.slice(5).trim()
          }
          // ": ping" 之类的注释行没有 data
          if (data)
            onEvent(name, JSON.parse(data))
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError")
        onEvent("error", { message: `连接中断：${(e as Error).message}` })
    } finally {
      streaming.value = false
      controller = null
    }
  }

  function stop() {
    controller?.abort()
  }

  onBeforeUnmount(stop)
  return { streaming, send, stop }
}
