import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import * as vue from "vue"
import { parseEvents } from "../src/http/sse.ts"

import { moduleLoader, require } from "./module-loader.ts"

const axios = require("axios").default

function body(parts: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({ start(controller) {
    for (const part of parts) controller.enqueue(encoder.encode(part))
    controller.close()
  } })
}

test("SSE 分块、CRLF、注释、多行 data 与 EOF", async () => {
  const events = []
  for await (const event of parseEvents(body([": ping\r", "\nevent: delta\r\ndata: 第一", "行\r\ndata: 第二行\r\n\r", "\nevent: done\ndata: {}"])))
    events.push(event)
  assert.deepEqual(events, [{ event: "delta", data: "第一行\n第二行" }, { event: "done", data: "{}" }])
})

function environment(fetcher: typeof fetch, websocket?: unknown, post?: () => Promise<unknown>) {
  const user = vue.reactive({ token: "old", sessionVersion: 1 })
  const messages: string[] = []
  const timers: { run: () => void, delay: number }[] = []
  const unmount: (() => void)[] = []
  class ApiError extends Error {
    code: number
    constructor(code: number, message: string) {
      super(message)
      this.code = code
    }
  }
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/http/axios": {
      ApiError,
      request: post,
      readEnvelope: (value: any) => {
        assert.equal(typeof value.code, "number")
        return value
      },
      rejectAuthentication: (version: number) => {
        if (version === user.sessionVersion) {
          user.sessionVersion++
          user.token = ""
        }
      }
    },
    "element-plus": { ElMessage: { error: (value: string) => messages.push(value) } }
  }, {
    ...vue,
    WebSocket: websocket,
    location: { protocol: "https:", host: "preview.test" },
    Error,
    ReadableStream,
    TextEncoder,
    fetch: fetcher,
    onBeforeUnmount: (callback: () => void) => unmount.push(callback),
    setTimeout: (run: () => void, delay: number) => {
      timers.push({ run, delay })
      return timers.length
    },
    clearTimeout() {}
  })
  return { load, user, messages, timers, unmount }
}

function response(text: string) {
  return new Response(body([text]), { headers: { "Content-Type": "text/event-stream" } })
}

test("旧流响应不触发回调；Chat POST 失败明确拒绝且不重发", async () => {
  const pending = Promise.withResolvers<Response>()
  const env = environment(async () => pending.promise)
  let called = 0
  const operation = env.load("@/http/stream").consumeStream("/api/news/stream", {}, 1, () => called++, () => called++)
  const rejected = assert.rejects(operation, (error: any) => axios.isCancel(error))
  env.user.sessionVersion++
  pending.resolve(response("event: news\ndata: {}\n\n"))
  await rejected
  assert.equal(called, 0)

  let requests = 0
  const events: string[] = []
  const chatEnv = environment(async (_, init) => {
    requests++
    assert.equal(init?.method, "POST")
    assert.equal((init?.headers as Record<string, string>).Authorization, "Bearer old")
    return response("event: error\ndata: {\"message\":\"模型失败\"}\n\n")
  })
  const chat = chatEnv.load("@/pages/chat/composables/useChatStream").useChatStream()
  await assert.rejects(chat.send({ text: "test" }, (event: { name: string }) => events.push(event.name)), /模型失败/)
  assert.deepEqual(events, ["error"])
  assert.equal(requests, 1)
  assert.equal(chatEnv.timers.length, 0)
  assert.equal(chat.streaming.value, false)
})

test("只读订阅有限退避重连，403 停止，当前身份保留", async () => {
  let calls = 0
  const env = environment(async () => {
    calls++
    if (calls < 3) throw new Error("network unavailable")
    return new Response(JSON.stringify({ code: 403, data: null, message: "权限不足" }), { status: 403 })
  })
  const source = env.load("@/http/useEventSource").useEventSource(env.load("@@/apis/news").subscribeNewsApi, () => {})
  await setImmediate()
  assert.equal(env.timers[0].delay, 1000)
  env.timers[0].run()
  await setImmediate()
  assert.equal(env.timers[1].delay, 2000)
  env.timers[1].run()
  await setImmediate()
  assert.equal(env.timers.length, 2)
  assert.equal(env.user.token, "old")
  assert.equal(source.connected.value, false)
  assert.deepEqual(env.messages, ["network unavailable", "权限不足"])
})

test("服务端 auth_error 清理当前身份；换会话立即 abort 旧 Chat", async () => {
  const env = environment(async () => response("event: auth_error\ndata: {\"code\":401,\"message\":\"已注销\"}\n\n"))
  env.load("@/http/useEventSource").useEventSource(env.load("@@/apis/news").subscribeNewsApi, () => assert.fail("不能继续推送"))
  await setImmediate()
  assert.equal(env.user.token, "")
  assert.equal(env.timers.length, 0)
  let signal: AbortSignal | undefined
  const pending = Promise.withResolvers<Response>()
  const chatEnv = environment(async (_, init) => {
    signal = init?.signal as AbortSignal
    return pending.promise
  })
  const chat = chatEnv.load("@/pages/chat/composables/useChatStream").useChatStream()
  const sending = chat.send({}, () => assert.fail("旧会话不能回调"))
  const rejected = assert.rejects(sending, (error: any) => axios.isCancel(error))
  chatEnv.user.sessionVersion++
  assert.equal(signal?.aborted, true)
  pending.resolve(response("event: done\ndata: {}\n\n"))
  await rejected
})

test("行情重连申请新票据，丢弃旧连接消息，切换会话关闭连接", async () => {
  const sockets: FakeSocket[] = []
  class FakeSocket {
    static OPEN = 1
    readyState = 0
    sent: unknown[] = []
    onopen = () => {}
    onmessage = (_: any) => {}
    onclose = (_: any) => {}
    constructor(url: string, protocols: string[]) {
      assert.equal(url, "wss://preview.test/api/market/ws")
      assert.equal(protocols[0], "alphapulse.market.v1")
      assert.equal(protocols[1], `ticket.ticket-${sockets.length + 1}`)
      sockets.push(this)
    }

    send(value: string) {
      this.sent.push(JSON.parse(value))
    }

    close() {
      this.readyState = 3
    }
  }
  let tickets = 0
  const env = environment(async () => assert.fail("不使用 fetch 连接 WS"), FakeSocket, async () => ({ ticket: `ticket-${++tickets}`, expires_in: 30 }))
  const prices: unknown[] = []
  const stream = env.load("@/pages/market/composables/useMarketSocket").useMarketSocket(() => {}, (price: unknown) => prices.push(price))
  stream.subscribe({ market: "um", symbol: "BTCUSDT", interval: "15m" })
  await setImmediate()
  sockets[0].readyState = 1
  sockets[0].onopen()
  assert.equal(stream.connected.value, true)
  assert.equal((sockets[0].sent[0] as any).symbol, "BTCUSDT")
  sockets[0].onclose({ code: 1011 })
  env.timers[0].run()
  await setImmediate()
  assert.equal(tickets, 2)
  sockets[0].onmessage({ data: JSON.stringify({ type: "ticker", market: "um", symbol: "BTCUSDT", data: 1 }) })
  sockets[1].onmessage({ data: JSON.stringify({ type: "ticker", market: "um", symbol: "BTCUSDT", data: 2 }) })
  assert.deepEqual(prices, [2])
  env.user.sessionVersion++
  assert.equal(sockets[1].readyState, 3)
  assert.equal(stream.connected.value, false)
})
