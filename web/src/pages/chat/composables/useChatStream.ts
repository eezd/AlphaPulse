import type { ChatStreamHandler, SendChatRequestData } from "@@/apis/chat/type"
import { sendChatApi } from "@@/apis/chat"
import { streamCanceled } from "@/http/stream"
import { useUserStore } from "@/pinia/stores/user"

/** Chat POST 只发送一次；失败由聊天区显示，拒绝 Promise 阻止后续成功处理。 */
export function useChatStream() {
  const streaming = ref(false)
  const user = useUserStore()
  let controller: AbortController | undefined

  async function send(body: SendChatRequestData, onEvent: ChatStreamHandler) {
    if (streaming.value)
      throw new Error("正在生成")
    const version = user.sessionVersion
    const request = new AbortController()
    controller = request
    streaming.value = true
    try {
      let finished = false
      await sendChatApi(body, request.signal, version, (event) => {
        if (event.name === "error")
          throw new Error(event.data.message)
        if (event.name === "done") finished = true
        onEvent(event)
      })
      if (!finished) throw new Error("生成连接提前中断，请核对已保存的回复")
    } catch (error) {
      if (!streamCanceled(error) && version === user.sessionVersion)
        onEvent({ name: "error", data: { message: error instanceof Error ? error.message : "生成失败" } })
      throw error
    } finally {
      if (controller === request) {
        streaming.value = false
        controller = undefined
      }
    }
  }

  function stop() {
    controller?.abort()
  }
  watch(() => user.sessionVersion, stop, { flush: "sync" })
  onBeforeUnmount(stop)
  return { streaming, send, stop }
}
