import type { Market } from "@/apis/market"

export interface KlineMessage {
  t: number
  o: string
  h: string
  l: string
  c: string
  v: string
  q: string
  x: boolean
}

export interface TickerMessage {
  c: string
  o: string
  h: string
  l: string
  v: string
  q: string
  E: number
}

export interface Subscription {
  market: Market
  symbol: string
  interval: string
}

const RETRY_MAX_MS = 30_000

/** 行情 WebSocket：断线指数退避重连，重连后自动恢复当前订阅；切换订阅后丢弃旧订阅的残留消息 */
export function useMarketSocket(onKline: (k: KlineMessage) => void, onTicker: (t: TickerMessage) => void) {
  const connected = ref(false)
  let socket: WebSocket | undefined
  let current: Subscription | undefined
  let retryMs = 1000
  let closed = false
  let retryTimer: ReturnType<typeof setTimeout> | undefined

  function connect() {
    // 组件已卸载时不再建立连接
    if (closed)
      return
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/market/ws`)
    socket.onopen = () => {
      connected.value = true
      retryMs = 1000
      if (current)
        socket!.send(JSON.stringify(current))
    }
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data)
      if (!current || message.market !== current.market || message.symbol !== current.symbol)
        return
      if (message.type === "kline" && message.interval === current.interval)
        onKline(message.data)
      else if (message.type === "ticker")
        onTicker(message.data)
    }
    socket.onclose = () => {
      connected.value = false
      if (closed)
        return
      retryTimer = setTimeout(connect, retryMs)
      retryMs = Math.min(retryMs * 2, RETRY_MAX_MS)
    }
  }

  function subscribe(subscription: Subscription) {
    current = subscription
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(subscription))
  }

  connect()
  onBeforeUnmount(() => {
    closed = true
    clearTimeout(retryTimer)
    socket?.close()
  })
  return { connected, subscribe }
}
