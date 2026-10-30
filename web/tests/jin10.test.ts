import assert from "node:assert/strict"
import test from "node:test"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

function setup(overrides: Record<string, unknown> = {}) {
  const user = { sessionVersion: 1 }
  const unmountHooks: (() => void)[] = []
  const onBeforeUnmount = (hook: () => void) => unmountHooks.push(hook)
  const cancelled: string[] = []
  const messages: string[] = []
  const timers: (() => Promise<void>)[] = []
  const load = moduleLoader({
    "vue": { ...vue, onBeforeUnmount },
    "@/pinia/stores/user": { useUserStore: () => user },
    "@@/apis/jin10": {
      createJin10QrApi: async () => ({ task_id: "a", image: "png", expires_at: "later" }),
      pollJin10QrApi: async () => ({ state: "connected" }),
      cancelJin10QrApi: async (id: string) => cancelled.push(id),
      getJin10Api: async () => ({ connected: true, sync_state: "awaiting_validation" }),
      disconnectJin10Api: async () => {},
      ...overrides
    }
  }, {
    ...vue,
    onMounted() {},
    onBeforeUnmount,
    setTimeout: (fn: (...args: unknown[]) => Promise<void>, _delay: number, ...args: unknown[]) => {
      timers.push(() => fn(...args))
      return timers.length
    },
    clearTimeout() {},
    ElMessage: { success: (message: string) => messages.push(message) },
    ElMessageBox: { confirm: async () => {} }
  })
  return {
    page: load("@/pages/settings/components/Jin10Settings.vue").default.setup({}, { expose() {} }),
    cancelled,
    messages,
    timers,
    unmount: () => unmountHooks.forEach(hook => hook()),
    invalidate: () => {
      user.sessionVersion++
    }
  }
}

test("QR generation is serialized and closing cancels a late response", async () => {
  const pending = Promise.withResolvers<any>()
  let creates = 0
  const env = setup({ createJin10QrApi: () => {
    creates++
    return pending.promise
  } })
  const first = env.page.start()
  await env.page.start()
  assert.equal(creates, 1)
  env.page.visible.value = false
  await vue.nextTick()
  pending.resolve({ task_id: "old", image: "png", expires_at: "later" })
  await first
  assert.equal(env.page.qr.value, undefined)
  assert.deepEqual(env.cancelled, ["old"])
  assert.equal(env.page.busy.value, false)
  assert.equal(env.timers.length, 0)
})

test("a closed QR dialog ignores a late successful poll", async () => {
  const pending = Promise.withResolvers<any>()
  const env = setup({ pollJin10QrApi: () => pending.promise })
  await env.page.start()
  const polling = env.timers[0]()
  env.page.visible.value = false
  await vue.nextTick()
  pending.resolve({ state: "connected" })
  await polling
  assert.deepEqual(env.messages, [])
  assert.equal(env.page.status.value, undefined)
})

test("successful login reports connection without claiming calendar sync", async () => {
  const env = setup()
  await env.page.start()
  await env.timers[0]()
  assert.equal(env.page.visible.value, false)
  assert.equal(env.page.status.value.sync_state, "awaiting_validation")
  assert.match(env.messages[0], /待核验/)
})

test("session changes discard QR responses and never cancel under a new identity", async () => {
  const pending = Promise.withResolvers<any>()
  const env = setup({ createJin10QrApi: () => pending.promise })
  const first = env.page.start()
  env.invalidate()
  pending.resolve({ task_id: "old-session", image: "png", expires_at: "later" })
  await first
  assert.equal(env.page.qr.value, undefined)
  assert.deepEqual(env.cancelled, [])
})

test("unmount cancels the QR after the real session scope has disposed", async () => {
  const env = setup()
  await env.page.start()
  env.unmount()
  await env.timers[0]()
  assert.deepEqual(env.cancelled, ["a"])
  assert.equal(env.page.qr.value, undefined)
  assert.deepEqual(env.messages, [])
})

test("a QR created after unmount is cancelled without changing the disposed page", async () => {
  const pending = Promise.withResolvers<any>()
  const env = setup({ createJin10QrApi: () => pending.promise })
  const starting = env.page.start()
  env.unmount()
  pending.resolve({ task_id: "late", image: "png", expires_at: "later" })
  await starting
  assert.deepEqual(env.cancelled, ["late"])
  assert.equal(env.page.qr.value, undefined)
  assert.equal(env.timers.length, 0)
})

test("unmount never cancels a task under a changed identity", async () => {
  const env = setup()
  await env.page.start()
  env.invalidate()
  env.unmount()
  assert.deepEqual(env.cancelled, [])
})

test("an older status response cannot overwrite a newer response", async () => {
  const older = Promise.withResolvers<any>()
  let requests = 0
  const env = setup({ getJin10Api: () => ++requests === 1 ? older.promise : Promise.resolve({ connected: false }) })
  const loading = env.page.load()
  await env.page.load()
  older.resolve({ connected: true })
  await loading
  assert.equal(env.page.status.value.connected, false)
  assert.equal(env.page.failed.value, false)
})

test("an older status error cannot replace a newer successful response", async () => {
  const older = Promise.withResolvers<any>()
  let requests = 0
  const env = setup({ getJin10Api: () => ++requests === 1 ? older.promise : Promise.resolve({ connected: false }) })
  const loading = env.page.load()
  await env.page.load()
  older.reject(new Error("old request failed"))
  await loading
  assert.equal(env.page.status.value.connected, false)
  assert.equal(env.page.failed.value, false)
})

test("disconnect invalidates status requests before its API response", async () => {
  const oldStatus = Promise.withResolvers<any>()
  const removing = Promise.withResolvers<void>()
  let requests = 0
  const env = setup({
    getJin10Api: () => ++requests === 1 ? oldStatus.promise : Promise.resolve({ connected: false }),
    disconnectJin10Api: () => removing.promise
  })
  const loading = env.page.load()
  const disconnecting = env.page.disconnect()
  await vue.nextTick()
  oldStatus.resolve({ connected: true })
  await loading
  assert.equal(env.page.status.value, undefined)
  removing.resolve()
  await disconnecting
  assert.equal(env.page.status.value.connected, false)
})

test("a stale disconnect confirmation cannot cancel a newer QR", async () => {
  const env = setup()
  const disconnecting = env.page.disconnect()
  await env.page.start()
  await disconnecting
  assert.equal(env.page.qr.value.task_id, "a")
  assert.deepEqual(env.cancelled, [])
})
