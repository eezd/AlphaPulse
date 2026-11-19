import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

const plain = (value: unknown) => JSON.parse(JSON.stringify(value))
function harness(
  options: {
    confirm?: () => Promise<any>
    list?: () => Promise<any>
    post?: () => Promise<any>
    analysis?: (id: number) => Promise<any>
  } = {}
) {
  let current = true
  let mounted = () => {}
  let unmount = () => {}
  let confirms = 0
  let uuid = 0
  const requests: any[] = []
  const navigation: any[] = []
  const events: any[] = []
  const timers = new Map<number, () => void>()
  const stops: (() => void)[] = []
  const document = { hidden: false }
  const route = vue.reactive({ query: {} })
  const load = moduleLoader(
    {
      "@@/apis/briefings": {
        kinds: {},
        states: {},
        listReportsApi: () =>
          options.list?.() ?? Promise.resolve({ items: [], total: 0 }),
        listNotificationsApi: async () => ({ items: [], total: 0 }),
        generateReportApi: (body: any) => {
          requests.push(plain(body))
          return options.post?.() ?? Promise.resolve({ id: 42 })
        },
        getAnalysisApi: (id: number) =>
          options.analysis?.(id) ?? Promise.resolve({ id }),
        retryAnalysisApi: (id: number) => {
          requests.push(id)
          return options.post?.() ?? Promise.resolve({ id: 84 })
        }
      },
      "@@/composables/useSessionScope": {
        useSessionScope: () => ({ isCurrent: () => current })
      },
      "./components/ReportDetail.vue": {},
      "element-plus": {
        ElMessage: { warning() {} },
        ElMessageBox: { confirm: async () => {} }
      }
    },
    {
      ...vue,
      document,
      watch(...args: Parameters<typeof vue.watch>) {
        const stop = vue.watch(...args)
        stops.push(stop)
        return stop
      },
      useRoute: () => route,
      useRouter: () => ({
        replace(value: any) {
          navigation.push(plain(value))
          return Promise.resolve()
        }
      }),
      crypto: { randomUUID: () => `request-${++uuid}` },
      ElMessage: { warning() {} },
      ElMessageBox: {
        confirm: () => {
          confirms++
          return options.confirm?.() ?? Promise.resolve()
        }
      },
      onMounted(fn: () => void) {
        mounted = fn
      },
      onBeforeUnmount(fn: () => void) {
        unmount = fn
      },
      setInterval(fn: () => void) {
        timers.set(1, fn)
        return 1
      },
      clearInterval(id: number) {
        timers.delete(id)
      }
    }
  )
  function component(path: string, props = {}) {
    return load(path).default.setup(props, {
      expose() {},
      emit: (...args: any[]) => events.push(plain(args))
    })
  }
  return {
    component,
    requests,
    navigation,
    events,
    timers,
    document,
    get confirms() {
      return confirms
    },
    mount() {
      mounted()
    },
    invalidate() {
      current = false
    },
    close() {
      current = false
      unmount()
      stops.forEach(stop => stop())
    }
  }
}

test("补报确认期间只提交一次，并保留日期、类型和 UUID 快照", async (t) => {
  const confirm = Promise.withResolvers<void>()
  const h = harness({ confirm: () => confirm.promise })
  t.after(h.close)
  const page = h.component("@/pages/reports/index.vue")
  Object.assign(page.generate, {
    kind: "morning",
    report_date: "2026-11-01",
    regenerate: false
  })
  const first = page.submit()
  await page.submit()
  Object.assign(page.generate, {
    kind: "daily",
    report_date: "2026-11-02",
    regenerate: true
  })
  assert.equal(h.confirms, 1)
  confirm.resolve()
  await first
  assert.deepEqual(h.requests, [
    {
      kind: "morning",
      report_date: "2026-11-01",
      regenerate: false,
      request_id: "request-1"
    }
  ])
  assert.deepEqual(h.navigation, [{ path: "/reports", query: { report: 42 } }])
  assert.equal(page.busy.value, false)
})

test("取消或会话失效后的补报确认不会发送请求", async (t) => {
  const confirm = Promise.withResolvers<void>()
  const h = harness({ confirm: () => confirm.promise })
  t.after(h.close)
  const page = h.component("@/pages/reports/index.vue")
  page.generate.report_date = "2026-11-01"
  const request = page.submit()
  h.invalidate()
  confirm.resolve()
  await request
  assert.equal(h.requests.length, 0)
})

test("补报响应未知时刷新列表而不自动重复 POST", async (t) => {
  let reads = 0
  const h = harness({
    post: async () => {
      throw new Error("unknown")
    },
    list: async () => {
      reads++
      return { items: [], total: 0 }
    }
  })
  t.after(h.close)
  const page = h.component("@/pages/reports/index.vue")
  page.generate.report_date = "2026-11-01"
  await page.submit()
  assert.equal(h.requests.length, 1)
  assert.equal(reads, 1)
  assert.equal(page.busy.value, false)
})

test("较晚返回的旧查询或卸载后的响应不会覆盖报告列表", async (t) => {
  const first = Promise.withResolvers<any>()
  const second = Promise.withResolvers<any>()
  let reads = 0
  const h = harness({
    list: () => (++reads === 1 ? first.promise : second.promise)
  })
  t.after(h.close)
  const page = h.component("@/pages/reports/index.vue")
  const old = page.load()
  const latest = page.load()
  second.resolve({ items: [{ id: 2 }], total: 1 })
  await latest
  first.resolve({ items: [{ id: 1 }], total: 1 })
  await old
  assert.equal(page.rows.value[0].id, 2)
  h.invalidate()
  await page.load()
  assert.equal(reads, 2)
})

test("报告可见时轮询，隐藏和卸载后不再刷新", async (t) => {
  let reads = 0
  const h = harness({
    list: async () => {
      reads++
      return { items: [], total: 0 }
    }
  })
  t.after(h.close)
  h.component("@/pages/reports/index.vue")
  h.mount()
  await setImmediate()
  h.document.hidden = true
  h.timers.get(1)!()
  assert.equal(reads, 1)
  h.document.hidden = false
  h.timers.get(1)!()
  assert.equal(reads, 2)
  h.close()
  assert.equal(h.timers.size, 0)
})

test("分析版本切换丢弃旧详情；确认期间切换版本不重做错误新闻", async (t) => {
  const old = Promise.withResolvers<any>()
  const confirm = Promise.withResolvers<void>()
  const h = harness({
    analysis: id => (id === 1 ? old.promise : Promise.resolve({ id })),
    confirm: () => confirm.promise
  })
  t.after(h.close)
  const props = vue.reactive({ id: 1 })
  const page = h.component("@/pages/home/components/AnalysisDetail.vue", props)
  props.id = 2
  await vue.nextTick()
  await Promise.resolve()
  old.resolve({ id: 1 })
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(page.detail.value.id, 2)
  const retry = page.retry()
  await page.retry()
  assert.equal(h.confirms, 1)
  props.id = 3
  await vue.nextTick()
  confirm.resolve()
  await retry
  assert.equal(h.requests.length, 0)
})
