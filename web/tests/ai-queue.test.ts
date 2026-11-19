import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

const first = { task_id: "11111111111141118111111111111111", wait_id: "22222222222242228222222222222222" }
const second = { task_id: "33333333333343338333333333333333", wait_id: "44444444444444448444444444444444" }
function snapshot(target = first) {
  return { checked_at: new Date().toISOString(), worker_heartbeat_at: new Date().toISOString(), progress_status: "available", items: [{
    claim: { kind: "analysis", id: 10 },
    task_id: target.task_id,
    stage: "analysis",
    analysis_id: 10,
    report: null,
    news: [{ id: 30, title: "正在提取的长新闻标题", source_code: "test", published_at: new Date().toISOString(), url: "https://example.invalid/news" }],
    started_at: new Date(Date.now() - 20000).toISOString(),
    can_retry_now: true,
    progress: { phase: "等待模型重试", wait_id: target.wait_id, retry_at: new Date(Date.now() + 180000).toISOString(), retry_attempt: 6, current_news_id: 30 }
  }] }
}
const plain = (value: unknown) => JSON.parse(JSON.stringify(value))
async function flush() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}
function harness(options: { get?: () => Promise<any>, reset?: (targets: any[]) => Promise<any>, confirm?: () => Promise<any>, clockOffset?: number } = {}) {
  let current = true
  let monotonicTime = 0
  let clockOffset = options.clockOffset ?? 0
  let mounted = () => {}
  let unmount = () => {}
  const timers = new Map<number, { callback: () => any, delay: number }>()
  let nextId = 0
  const listeners = new Map<string, () => void>()
  const document = { hidden: false, addEventListener(name: string, callback: () => void) {
    listeners.set(name, callback)
  }, removeEventListener(name: string) {
    listeners.delete(name)
  } }
  const user = vue.reactive({ sessionVersion: 1 })
  const sent: any[] = []
  const events: any[] = []
  let reads = 0
  const load = moduleLoader({
    "@@/apis/briefings": {
      getAiQueueApi: () => {
        reads++
        return options.get?.() ?? Promise.resolve(snapshot())
      },
      resetAiWaitsApi: (targets: any[]) => {
        sent.push(plain(targets))
        return options.reset?.(targets) ?? Promise.resolve({ scheduled: targets, already_requested: [], skipped: [] })
      }
    },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => current }) },
    "@/pinia/stores/user": { useUserStore: () => user }
  }, {
    ...vue,
    Date: class extends Date {
      static now() {
        return Date.now() + clockOffset
      }
    },
    performance: { now: () => monotonicTime },
    document,
    defineEmits: () => (name: string, id: number) => events.push([name, id]),
    ElMessageBox: { confirm: options.confirm ?? (() => Promise.resolve()) },
    onMounted(fn: () => void) {
      mounted = fn
    },
    onBeforeUnmount(fn: () => void) {
      unmount = fn
    },
    setTimeout(callback: () => void, delay: number) {
      timers.set(++nextId, { callback, delay })
      return nextId
    },
    clearTimeout(id: number) {
      timers.delete(id)
    },
    setInterval(callback: () => void, delay: number) {
      timers.set(++nextId, { callback, delay })
      return nextId
    },
    clearInterval(id: number) {
      timers.delete(id)
    }
  })
  const page = load("@/pages/home/components/AiQueue.vue").default.setup({}, { expose() {}, emit: (name: string, id: number) => events.push([name, id]) })
  return { page, sent, events, timers, document, user, get reads() {
    return reads
  }, mounted, advance(milliseconds: number, wallClockShift = milliseconds) {
    monotonicTime += milliseconds
    clockOffset += wallClockShift
    for (const timer of timers.values()) {
      if (timer.delay === 1000) timer.callback()
    }
  }, hide(hidden: boolean) {
    document.hidden = hidden
    listeners.get("visibilitychange")?.()
  }, invalidate() {
    current = false
    user.sessionVersion++
  }, close() {
    unmount()
  } }
}

test("队列读取、倒计时、精确单条立即重试及受理状态", async (t) => {
  const h = harness()
  t.after(h.close)
  h.mounted()
  await flush()
  assert.equal(h.page.queue.value.items[0].news[0].title, "正在提取的长新闻标题")
  assert.equal(h.page.seconds(h.page.queue.value.items[0].progress.retry_at, true), "180 秒")
  await h.page.retry(h.page.targetFor(h.page.queue.value.items[0]))
  assert.deepEqual(h.sent, [[first]])
  assert.equal(h.reads, 2)
  assert.match(h.page.notice.value, /已请求立即重试 1 个/)
  assert.ok(h.page.requested.value.includes(h.page.key(first)))
  h.advance(6000)
  assert.equal(h.page.targets().length, 0)
  h.page.retryItem(h.page.queue.value.items[0])
  assert.equal(h.sent.length, 1)
})

test("已有心跳年龄计入有效期，过期后禁用单条与批量重试", async (t) => {
  const data = snapshot()
  data.worker_heartbeat_at = new Date(Date.parse(data.checked_at) - 4500).toISOString()
  const h = harness({ get: async () => data })
  t.after(h.close)
  h.mounted()
  await flush()
  assert.equal(h.page.fresh(), true)
  assert.equal(h.page.targets().length, 1)
  h.advance(500)
  assert.equal(h.page.fresh(), true)
  h.advance(500)
  assert.equal(h.page.fresh(), false)
  assert.equal(h.page.targets().length, 0)
  h.page.retryItem(h.page.queue.value.items[0])
  await h.page.retry()
  assert.equal(h.sent.length, 0)
})

for (const clockOffset of [-20000, 20000]) {
  test(`客户端时钟偏差 ${clockOffset}ms 及校时不改变进度有效期`, async (t) => {
    const h = harness({ clockOffset })
    t.after(h.close)
    h.mounted()
    await flush()
    assert.equal(h.page.fresh(), true)
    assert.equal(h.page.targets().length, 1)
    h.advance(4000, 0)
    assert.equal(h.page.fresh(), true)
    assert.equal(h.page.targets().length, 1)
    h.advance(1001, 0)
    assert.equal(h.page.fresh(), false)
    assert.equal(h.page.targets().length, 0)
    await h.page.refresh()
    h.advance(1000, -120000)
    assert.equal(h.page.fresh(), true)
    h.advance(1000, 240000)
    assert.equal(h.page.fresh(), true)
    h.advance(3001, 0)
    assert.equal(h.page.fresh(), false)
  })
}

test("批量确认期间目标变化仍提交确认前的精确等待", async (t) => {
  const confirmation = Promise.withResolvers<void>()
  let data = snapshot()
  const h = harness({ get: async () => data, confirm: () => confirmation.promise })
  t.after(h.close)
  await h.page.refresh()
  const request = h.page.retry()
  data = snapshot(second)
  await h.page.refresh()
  confirmation.resolve()
  await request
  assert.deepEqual(h.sent, [[first]])
  assert.notEqual(h.sent[0][0].wait_id, second.wait_id)
})

test("立即重试超时仅提示结果未确认并刷新，不自动重发", async (t) => {
  const h = harness({ reset: async () => {
    throw new Error("503 unknown")
  } })
  t.after(h.close)
  await h.page.refresh()
  await h.page.retry(first)
  assert.equal(h.sent.length, 1)
  assert.equal(h.reads, 2)
  assert.match(h.page.notice.value, /结果未确认/)
  assert.equal(h.page.busy.value, false)
})

test("失效目标明确提示且无进度时禁用操作，失败不伪装空队列", async (t) => {
  let fail = false
  let data: any = snapshot()
  const h = harness({ get: async () => {
    if (fail) throw new Error("read failed")
    return data
  }, reset: async targets => ({ scheduled: [], already_requested: [], skipped: targets.map(target => ({ ...target, reason: "not_waiting" })) }) })
  t.after(h.close)
  await h.page.refresh()
  await h.page.retry(first)
  assert.match(h.page.notice.value, /已结束或失效 1 个/)
  data = { ...snapshot(), progress_status: "stale" }
  await h.page.refresh()
  assert.equal(h.page.targets().length, 0)
  fail = true
  await h.page.refresh()
  assert.equal(h.page.failed.value, true)
  assert.equal(h.page.queue.value.items.length, 1)
})

test("隐藏停止计时及轮询，恢复后立即刷新，卸载清除全部定时器", async () => {
  const h = harness()
  h.mounted()
  await flush()
  assert.equal(h.reads, 1)
  assert.deepEqual([...h.timers.values()].map(timer => timer.delay).sort(), [1000, 5000])
  h.hide(true)
  assert.equal(h.timers.size, 0)
  await h.page.refresh()
  assert.equal(h.reads, 1)
  h.hide(false)
  await flush()
  assert.equal(h.reads, 2)
  h.close()
  assert.equal(h.timers.size, 0)
  assert.equal(h.page.queue.value, null)
})

for (const reason of ["unmount", "session", "hidden"]) {
  for (const outcome of ["success", "failure"]) {
    test(`${reason} 后丢弃延迟${outcome}响应`, async (t) => {
      const pending = Promise.withResolvers<any>()
      const h = harness({ get: () => pending.promise })
      t.after(h.close)
      const request = h.page.refresh()
      if (reason === "unmount") {
        h.close()
      } else if (reason === "session") {
        h.invalidate()
        await vue.nextTick()
      } else {
        h.mounted()
        h.hide(true)
      }
      if (outcome === "success") pending.resolve(snapshot())
      else pending.reject(new Error("old response"))
      await request
      await flush()
      assert.equal(h.page.queue.value, null)
      assert.equal(h.page.failed.value, false)
      assert.equal(h.timers.size, 0)
    })
  }
}

test("过期响应不能覆盖较新的队列结果", async (t) => {
  const old = Promise.withResolvers<any>()
  let call = 0
  const h = harness({ get: () => ++call === 1 ? old.promise : Promise.resolve(snapshot(second)) })
  t.after(h.close)
  const firstRequest = h.page.refresh()
  await h.page.refresh()
  old.resolve(snapshot(first))
  await firstRequest
  assert.equal(h.page.queue.value.items[0].task_id, second.task_id)
})

test("新页签复用详情入口，并区分不可用和真实空队列", () => {
  const source = readFileSync(new URL("../src/pages/home/components/AiQueue.vue", import.meta.url), "utf8")
  assert.match(source, /emit\('analysis', item.analysis_id\)/)
  assert.match(source, /path: '\/reports', query: \{ report: item.report.id \}/)
  assert.match(source, /实时进度暂不可用/)
  assert.match(source, /当前没有执行中的 AI 任务/)
  assert.match(source, /报告生成/)
  const home = readFileSync(new URL("../src/pages/home/index.vue", import.meta.url), "utf8")
  assert.match(home, /<AiQueue/)
  assert.match(home, /AnalysisDetail/)
  const routes = readFileSync(new URL("../src/router/routes.ts", import.meta.url), "utf8")
  assert.doesNotMatch(routes, /pages\/history|旧研究历史/)
})
