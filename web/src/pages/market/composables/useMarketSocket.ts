import type { KlineMessage, MarketMessage, Subscription, TickerMessage } from "@@/apis/market/type"

import { createWebSocketTicketApi } from "@@/apis/auth"
import { createMarketSocketApi } from "@@/apis/market"
import axios from "axios"
import { ElMessage } from "element-plus"
import { ApiError, rejectAuthentication } from "@/http/axios"
import { useUserStore } from "@/pinia/stores/user"

const RETRY_MAX_MS = 30_000

/** 行情 WebSocket：断线指数退避重连，重连后自动恢复当前订阅；切换订阅后丢弃旧订阅的残留消息 */
export function useMarketSocket(onKline: (k: KlineMessage) => void, onTicker: (t: TickerMessage) => void) {
  const connected = ref(false)
  const user = useUserStore()
  const version = user.sessionVersion
  let socket: WebSocket | undefined
  let current: Subscription | undefined
  let retryMs = 1000
  let closed = false
  let retryTimer: ReturnType<typeof setTimeout> | undefined

  function retry() {
    if (closed || version !== user.sessionVersion)
      return
    retryTimer = setTimeout(connect, retryMs)
    retryMs = Math.min(retryMs * 2, RETRY_MAX_MS)
  }

  async function connect() {
    if (closed || version !== user.sessionVersion)
      return
    try {
      const { ticket } = await createWebSocketTicketApi()
      if (closed || version !== user.sessionVersion)
        return
      const ws = createMarketSocketApi(ticket)
      socket = ws
      const active = () => !closed && version === user.sessionVersion && socket === ws
      ws.onopen = () => {
        if (!active()) {
          ws.close()
          return
        }
        connected.value = true
        retryMs = 1000
        if (current) ws.send(JSON.stringify(current))
      }
      ws.onmessage = (event) => {
        if (!active()) return
        const message: MarketMessage = JSON.parse(event.data)
        if (message.type === "error") {
          ElMessage.error(message.message)
          return
        }
        if (!current || message.market !== current.market || message.symbol !== current.symbol)
          return
        if (message.type === "kline" && message.interval === current.interval)
          onKline(message.data)
        else if (message.type === "ticker")
          onTicker(message.data)
      }
      ws.onclose = (event) => {
        if (!active()) return
        connected.value = false
        if (event.code === 4401) {
          rejectAuthentication(version)
          return
        }
        retry()
      }
    } catch (error) {
      if (axios.isCancel(error) || (error instanceof ApiError && [401, 403].includes(error.code)))
        return
      retry()
    }
  }

  function subscribe(subscription: Subscription) {
    current = subscription
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(subscription))
  }

  void connect()
  function close() {
    closed = true
    clearTimeout(retryTimer)
    socket?.close()
    connected.value = false
  }
  watch(() => user.sessionVersion, close, { flush: "sync" })
  onBeforeUnmount(close)
  return { connected, subscribe }
}
