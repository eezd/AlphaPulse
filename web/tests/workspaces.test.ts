import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import axios from "axios"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

const globals = { ...vue, useRoute: () => ({ query: {} }), onMounted() {}, onBeforeUnmount() {}, onBeforeRouteLeave() {}, ElMessage: { success() {}, warning() {} } }

const tradingStatus = (limit = 120) => ({ exchange: "bybit", configured: true, provider_id: null, reasoning_effort: null, snapshot: null, today: { analyses: 10, limit, input_tokens: 0, output_tokens: 0 } })

test("分析详情只采用当前请求；关闭、卸载和旧失败都不能覆盖新详情", async () => {
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  let unmount = () => {}
  const props = vue.reactive({ analysisId: 1 })
  const load = moduleLoader({
    "vue": { ...vue, useModel: () => vue.ref(false) },
    "@@/apis/trading": { getAnalysisApi: () => {
      const request = Promise.withResolvers()
      pending.push(request)
      return request.promise
    } }
  }, { ...globals, onBeforeUnmount: (callback: () => void) => unmount = callback })
  const page = load("@/pages/trading/components/AnalysisDrawer.vue").default.setup(props, { expose() {} })
  page.visible.value = true
  await vue.nextTick()
  props.analysisId = 2
  await vue.nextTick()
  pending[1].resolve({ id: 2 })
  await setImmediate()
  pending[0].resolve({ id: 1 })
  await setImmediate()
  assert.equal(page.detail.value.id, 2)

  const old = page.load()
  const rejected = assert.rejects(old, /旧请求失败/)
  const current = page.load()
  pending[3].resolve({ id: 2 })
  await current
  pending[2].reject(new Error("旧请求失败"))
  await rejected
  assert.equal(page.failed.value, false)
  assert.equal(page.detail.value.id, 2)

  const closed = page.load()
  page.visible.value = false
  await vue.nextTick()
  pending[4].resolve({ id: 2 })
  await closed
  assert.equal(page.detail.value, undefined)
  assert.equal(page.loading.value, false)

  page.visible.value = true
  await vue.nextTick()
  unmount()
  pending[5].resolve({ id: 2 })
  await setImmediate()
  assert.equal(page.detail.value, undefined)
})

test("分析详情失败停止加载，明确失败并允许重试", async () => {
  let fail = true
  const load = moduleLoader({
    "vue": { ...vue, useModel: () => vue.ref(true) },
    "@@/apis/trading": { getAnalysisApi: async () => {
      if (fail) throw new Error("读取失败")
      return { id: 1 }
    } }
  }, globals)
  const page = load("@/pages/trading/components/AnalysisDrawer.vue").default.setup({ analysisId: 1 }, { expose() {} })
  await assert.rejects(page.load(), /读取失败/)
  assert.equal(page.loading.value, false)
  assert.equal(page.failed.value, true)
  fail = false
  await page.load()
  assert.equal(page.failed.value, false)
  assert.equal(page.detail.value.id, 1)
})

test("Skill 版本绑定当前对象；确认期间切换后不发布旧版本", async () => {
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const confirmation = Promise.withResolvers<void>()
  const writes: unknown[] = []
  const props = vue.reactive({ skill: { id: 1, published_version_no: null, latest_version_no: 1 } })
  const load = moduleLoader({
    "vue": { ...vue, useModel: () => vue.ref(false) },
    "@@/apis/skills": {
      listSkillVersionsApi: () => {
        const request = Promise.withResolvers()
        pending.push(request)
        return request.promise
      },
      publishSkillVersionApi: async (...args: unknown[]) => writes.push(args)
    },
    "@@/utils/text": { diffLines: () => [], formatTime: (value: string) => value },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ assertCurrent() {} }) }
  }, { ...globals, ElMessageBox: { confirm: () => confirmation.promise } })
  const page = load("@@/components/SkillVersionDrawer/index.vue").default.setup(props, { expose() {}, emit() {} })
  page.visible.value = true
  await vue.nextTick()
  props.skill = { id: 2, published_version_no: null, latest_version_no: 1 }
  await vue.nextTick()
  const version = { id: 22, version_no: 1, content: "B" }
  pending[1].resolve([version])
  await setImmediate()
  pending[0].resolve([{ id: 11, version_no: 1, content: "A" }])
  await setImmediate()
  assert.equal(page.versions.value[0].id, 22)

  const publishing = page.publish(version)
  const rejected = assert.rejects(publishing, error => axios.isCancel(error))
  props.skill = { id: 3, published_version_no: null, latest_version_no: 1 }
  await vue.nextTick()
  assert.equal(page.versions.value.length, 0)
  assert.equal(page.selectedId.value, null)
  confirmation.resolve()
  await rejected
  assert.equal(writes.length, 0)
  page.visible.value = false
  await vue.nextTick()
  pending[2].resolve([{ id: 33, version_no: 1, content: "C" }])
  await setImmediate()
  assert.equal(page.versions.value.length, 0)

  page.visible.value = true
  await vue.nextTick()
  pending[3].resolve([{ id: 33, version_no: 1, content: "C" }])
  await setImmediate()
  const failing = page.load()
  pending[4].reject(new Error("版本读取失败"))
  await assert.rejects(failing, /版本读取失败/)
  assert.equal(page.failed.value, true)
  assert.equal(page.loading.value, false)
  assert.equal(page.versions.value.length, 0)
  const retry = page.load()
  pending[5].resolve([{ id: 33, version_no: 1, content: "C" }])
  await retry
  assert.equal(page.failed.value, false)
  await page.publish(page.versions.value[0])
  assert.deepEqual(writes, [[3, 33]])
})

test("历史仓位失败不展示旧筛选结果；旧失败不影响新查询，重试可恢复", async () => {
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const load = moduleLoader({
    "vue": { ...vue, useModel: () => vue.ref(false) },
    "@@/apis/trading": { listPositionHistoryApi: () => {
      const request = Promise.withResolvers()
      pending.push(request)
      return request.promise
    } }
  }, globals)
  const page = load("@/pages/trading/components/PositionHistoryDrawer.vue").default.setup({}, { expose() {} })
  const first = page.load()
  pending[0].resolve({ items: [{ symbol: "BTCUSDT" }], total: 1 })
  await first
  page.filters.symbol = "ETHUSDT"
  const failing = page.load()
  assert.equal(page.result.value, undefined)
  pending[1].reject(new Error("读取失败"))
  await assert.rejects(failing, /读取失败/)
  assert.equal(page.failed.value, true)
  assert.equal(page.loading.value, false)
  assert.equal(page.result.value, undefined)

  const old = page.load()
  const rejected = assert.rejects(old, /旧请求失败/)
  const current = page.load()
  pending[3].resolve({ items: [{ symbol: "ETHUSDT" }], total: 1 })
  await current
  pending[2].reject(new Error("旧请求失败"))
  await rejected
  assert.equal(page.failed.value, false)
  assert.equal(page.result.value.items[0].symbol, "ETHUSDT")
})

test("交易配置草稿抵抗轮询、仅保存改动字段；失败保留草稿并拒绝重复提交", async () => {
  const pending = Promise.withResolvers<unknown>()
  const bodies: unknown[] = []
  let fail = false
  const props = vue.reactive({ status: tradingStatus(), providers: [] })
  const load = moduleLoader({ "@@/apis/trading": { patchConfigApi: (body: unknown) => {
    bodies.push(body)
    return fail ? Promise.reject(new Error("保存失败")) : pending.promise
  } } }, globals)
  const emitted: unknown[] = []
  const page = load("@/pages/trading/components/TradingConfig.vue").default.setup(props, { expose() {}, emit: (...args: unknown[]) => emitted.push(args) })
  page.formRef.value = { validate: async () => true, clearValidate() {} }
  page.form.daily_analysis_limit = 200
  props.status = tradingStatus(150)
  await vue.nextTick()
  assert.equal(page.form.daily_analysis_limit, 200)
  const saving = page.save()
  await page.save()
  await setImmediate()
  assert.equal(bodies.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(bodies[0])), { daily_analysis_limit: 200 })
  pending.resolve(tradingStatus(200))
  await saving
  assert.equal(page.dirty.value, false)
  assert.equal(emitted.length, 1)
  fail = true
  page.form.provider_id = 2
  await assert.rejects(page.save(), /保存失败/)
  assert.equal(page.form.provider_id, 2)
  assert.equal(page.dirty.value, true)
  assert.equal(page.saving.value, false)
  page.reset()
  assert.equal(page.form.daily_analysis_limit, 150)
})

test("模型表单按请求字段编辑；校验失败不请求，保存失败保留输入和弹窗", async () => {
  let calls = 0
  const load = moduleLoader({
    "@@/apis/settings": { patchProviderApi: async () => {
      calls++
      throw new Error("保存失败")
    } },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ assertCurrent() {} }) }
  }, globals)
  const page = load("@/pages/settings/components/ProviderSettings.vue").default.setup({}, { expose() {} })
  page.openEdit({ id: 1, name: "test", protocol: "openai", base_url: "https://example.test", api_key_hint: "abcd", model: "model", max_tokens: 8192, context_window: 200000, reasoning_effort: null, is_default: false, updated_at: "now" })
  assert.equal(page.form.value.api_key, "")
  assert.equal("api_key_hint" in page.form.value, false)
  assert.equal("updated_at" in page.form.value, false)
  page.formRef.value = { validate: async () => {
    throw new Error("校验失败")
  } }
  await assert.rejects(page.save(), /校验失败/)
  assert.equal(calls, 0)
  page.formRef.value = { validate: async () => true }
  page.form.value.name = "new-name"
  await assert.rejects(page.save(), /保存失败/)
  assert.equal(page.dialogVisible.value, true)
  assert.equal(page.form.value.name, "new-name")
  assert.equal(page.saving.value, false)
})

test("新闻搜索/页容量重置只有一次请求；失败区别于空数据且旧响应不覆盖新查询", async () => {
  const queries: { page: number }[] = []
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const load = moduleLoader({
    "@@/apis/news": { listNewsApi: (query: { page: number }) => {
      queries.push(query)
      const item = Promise.withResolvers()
      pending.push(item)
      return item.promise
    } },
    "@@/composables/useSourceNames": { useSourceNames: () => vue.ref({}) },
    "@@/utils/storage": { usePersisted: (_: string, value: unknown) => vue.ref(value) },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => true }) },
    "@/http/useEventSource": { useEventSource: () => ({ connected: vue.ref(false) }) }
  }, globals)
  const page = load("@/pages/news/composables/useNewsFeed").useNewsFeed()
  const first = page.load()
  const next = page.load()
  pending[1].resolve({ items: [{ id: 2 }], total: 1 })
  await next
  pending[0].resolve({ items: [{ id: 1 }], total: 1 })
  await first
  assert.equal(page.items.value[0].id, 2)
  page.page.value = 3
  await vue.nextTick()
  pending[2].resolve({ items: [], total: 0 })
  await setImmediate()
  page.search()
  await vue.nextTick()
  assert.equal(queries.length, 4)
  assert.equal(queries[3].page, 1)
  pending[3].resolve({ items: [], total: 0 })
  await setImmediate()
  page.page.value = 3
  await vue.nextTick()
  pending[4].resolve({ items: [], total: 0 })
  await setImmediate()
  page.pageSize.value = 50
  await vue.nextTick()
  assert.equal(queries.length, 6)
  assert.equal(queries[5].page, 1)
  pending[5].resolve({ items: [], total: 0 })
  await setImmediate()
  const failing = page.load()
  pending[6].reject(new Error("读取失败"))
  await assert.rejects(failing, /读取失败/)
  assert.equal(page.failed.value, true)
})

test("交易确认绑定发起币种；交易所或会话切换后拒绝旧操作", async () => {
  let confirm = Promise.withResolvers<void>()
  let active = true
  let exchange = "bybit"
  const writes: unknown[] = []
  const data = {
    status: vue.ref(tradingStatus()),
    symbols: vue.ref([]),
    selected: vue.ref("BTCUSDT"),
    events: vue.ref([]),
    eventsExhausted: vue.ref(true),
    latestAnalysis: vue.ref(null),
    livePlan: vue.ref(null),
    planOrders: vue.ref([]),
    current: vue.ref(null),
    loading: vue.ref(false),
    failed: vue.ref(false),
    refresh: async () => {},
    refreshEvents: async () => {},
    loadMoreEvents: async () => {}
  }
  const children = Object.fromEntries(["AddSymbolDialog", "AnalysisDrawer", "NewsSignalDrawer", "PlanCard", "PositionHistoryDrawer", "TradingConfig"].map(name => [`./components/${name}.vue`, {}]))
  const load = moduleLoader({
    ...children,
    "./composables/useTradingData": { useTradingData: () => data },
    "@@/apis/settings": {},
    "@@/apis/trading": {
      getStatusApi: async () => ({ ...tradingStatus(), exchange }),
      manualActionApi: async (...args: unknown[]) => writes.push(args)
    },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ assertCurrent() {
      if (!active) throw new Error("会话已切换")
    } }) }
  }, { ...globals, ElMessageBox: { confirm: () => confirm.promise } })
  const page = load("@/pages/trading/index.vue").default.setup({}, { expose() {} })
  const first = page.clearOrders()
  data.selected.value = "ETHUSDT"
  confirm.resolve()
  await first
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [["BTCUSDT", "cancel"]])
  confirm = Promise.withResolvers<void>()
  const second = page.clearOrders()
  exchange = "binance"
  confirm.resolve()
  await assert.rejects(second, /交易所已切换/)
  assert.equal(writes.length, 1)
  exchange = "bybit"
  confirm = Promise.withResolvers<void>()
  const third = page.closePosition("BTCUSDT")
  active = false
  confirm.resolve()
  await assert.rejects(third, /会话已切换/)
  assert.equal(writes.length, 1)
})

test("新闻 AI 详情绑定新闻修订版本，旧分析响应不能覆盖当前版本", async () => {
  const requests: { params: unknown, response: ReturnType<typeof Promise.withResolvers> }[] = []
  const props = vue.reactive({ newsId: 1, revision: 1 })
  const user = { sessionVersion: 1 }
  const load = moduleLoader({
    "@@/apis/news-research": { listNewsPackagesApi: (id: number, revision: number) => {
      const params = { news_id: id, revision_no: revision }
      const response = Promise.withResolvers()
      requests.push({ params, response })
      return response.promise
    } },
    "@/pinia/stores/user": { useUserStore: () => user }
  }, globals)
  const page = load("@/pages/news/components/NewsRisk.vue").default.setup(props, { expose() {} })
  props.revision = 2
  await vue.nextTick()
  assert.deepEqual(JSON.parse(JSON.stringify(requests[1].params)), { news_id: 1, revision_no: 2 })
  requests[1].response.resolve([{ id: 2 }])
  await setImmediate()
  requests[0].response.resolve([{ id: 1 }])
  await setImmediate()
  assert.equal(page.summaries.value[0].id, 2)
  props.newsId = 3
  await vue.nextTick()
  user.sessionVersion++
  requests[2].response.resolve([{ id: 3 }])
  await setImmediate()
  assert.equal(page.summaries.value.length, 0)
})

test("新闻 AI 详情等待期间自动刷新，完成后停止；隐藏、旧会话和卸载不继续请求", async () => {
  let tick = () => {}
  let unmount = () => {}
  let cleared = false
  let state = "pending"
  let requests = 0
  const document = { hidden: false }
  const user = { sessionVersion: 1 }
  const load = moduleLoader({
    "@@/apis/news-research": { listNewsPackagesApi: async () => {
      requests++
      return [{ state }]
    } },
    "@/pinia/stores/user": { useUserStore: () => user }
  }, {
    ...globals,
    document,
    onMounted(callback: () => void) {
      callback()
    },
    onBeforeUnmount(callback: () => void) {
      unmount = callback
    },
    setInterval(callback: () => void) {
      tick = callback
      return 1
    },
    clearInterval() {
      cleared = true
    }
  })
  const props = vue.reactive({ newsId: 1, revision: 1 })
  const page = load("@/pages/news/components/NewsRisk.vue").default.setup(props, { expose() {} })
  await setImmediate()
  assert.equal(page.summaries.value[0].state, "pending")
  document.hidden = true
  tick()
  assert.equal(requests, 1)
  document.hidden = false
  state = "done"
  tick()
  tick()
  await setImmediate()
  assert.equal(page.summaries.value[0].state, "done")
  assert.equal(requests, 2)
  tick()
  assert.equal(requests, 2)
  state = "pending"
  props.revision++
  await vue.nextTick()
  await setImmediate()
  user.sessionVersion++
  tick()
  assert.equal(requests, 3)
  user.sessionVersion--
  unmount()
  tick()
  assert.equal(requests, 3)
  assert.equal(cleared, true)
})

test("新闻列表只更新可见条目的匹配版本评级，保留滚动与新消息提示并丢弃旧页结果", async () => {
  const queries: Record<string, unknown>[] = []
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  let active = true
  let scrolls = 0
  const load = moduleLoader({
    "@@/apis/news": { listNewsApi: (query: Record<string, unknown>) => {
      queries.push(query)
      const response = Promise.withResolvers()
      pending.push(response)
      return response.promise
    } },
    "@@/composables/useSourceNames": { useSourceNames: () => vue.ref({}) },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => active }) },
    "@@/utils/storage": { usePersisted: (_: string, value: unknown) => vue.ref(value) },
    "@/http/useEventSource": { useEventSource: () => ({ connected: vue.ref(false) }) }
  }, globals)
  const page = load("@/pages/news/composables/useNewsFeed").useNewsFeed()
  page.listBox.value = { scrollTo() {
    scrolls++
  } }
  const initial = page.load()
  pending[0].resolve({ items: [{ id: 1, revision_no: 1 }, { id: 2, revision_no: 1, risk: { state: "processing" } }], total: 120 })
  await initial
  page.pendingCount.value = 7
  page.openedId.value = 1
  const refreshing = page.refreshRisk()
  assert.deepEqual(JSON.parse(JSON.stringify(queries[1])), { ids: [1, 2], limit: 100 })
  pending[1].resolve({ items: [{ id: 1, revision_no: 1, risk: { state: "done", stars: 5 } }, { id: 2, revision_no: 2, risk: { state: "done", stars: 1 } }], total: 2 })
  await refreshing
  assert.equal(page.items.value[0].risk.stars, 5)
  assert.equal(page.items.value[1].risk.state, "processing")
  assert.equal(page.total.value, 120)
  assert.equal(page.pendingCount.value, 7)
  assert.equal(page.openedId.value, 1)
  assert.equal(scrolls, 1)
  const old = page.refreshRisk()
  const current = page.load()
  pending[3].resolve({ items: [{ id: 3, revision_no: 1 }], total: 1 })
  await current
  pending[2].resolve({ items: [{ id: 3, revision_no: 1, risk: { state: "done" } }], total: 1 })
  await old
  assert.equal(page.items.value[0].risk, undefined)
  const previous = page.refreshRisk()
  active = false
  pending[4].resolve({ items: [{ id: 3, revision_no: 1, risk: { state: "done" } }], total: 1 })
  await previous
  assert.equal(page.items.value[0].risk, undefined)
  await page.refreshRisk()
  assert.equal(queries.length, 5)
})

test("事件详情忽略旧请求与旧会话，失败可重试", async () => {
  const user = vue.reactive({ sessionVersion: 1 })
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/common/apis/risk": { getNewsEventApi: () => {
      const request = Promise.withResolvers()
      pending.push(request)
      return request.promise
    } },
    "@@/utils/text": { formatTime: (value: string) => value }
  }, globals)
  const page = load("@/pages/home/components/EventResearch.vue").default.setup({}, { expose() {}, emit() {} })
  const first = page.open(1)
  const second = page.open(2)
  pending[1].resolve({ id: 2 })
  await second
  pending[0].reject(new Error("Old failure"))
  await first
  assert.equal(page.detail.value.id, 2)
  assert.equal(page.detailError.value, false)
  const failed = page.open(3)
  pending[2].reject(new Error("Failed"))
  await failed
  assert.equal(page.detailError.value, true)
  const retry = page.open(3)
  user.sessionVersion++
  await vue.nextTick()
  pending[3].resolve({ id: 3 })
  await retry
  assert.equal(page.detail.value, null)
  assert.equal(page.opened.value, false)
})

test("重整理预览的旧会话完成不能解除新操作的忙碌状态", async () => {
  const user = vue.reactive({ sessionVersion: 1 })
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/common/apis/risk": { previewReprocessApi: () => {
      const request = Promise.withResolvers()
      pending.push(request)
      return request.promise
    } },
    "@@/utils/text": { formatTime: (value: string) => value }
  }, globals)
  const page = load("@/pages/home/components/EventResearch.vue").default.setup({}, { expose() {}, emit() {} })
  page.range.value = [new Date("2026-11-01"), new Date("2026-11-02")]
  await vue.nextTick()
  const old = page.inspect()
  user.sessionVersion++
  await vue.nextTick()
  const current = page.inspect()
  pending[0].resolve({ articles: 1 })
  await old
  assert.equal(page.busy.value, true)
  assert.equal(page.preview.value, null)
  pending[1].resolve({ articles: 2 })
  await current
  assert.equal(page.busy.value, false)
  assert.equal(page.preview.value.articles, 2)
})

test("AI 队列隔离旧会话与旧筛选响应，轮询隐藏页面时暂停并在卸载后清理", async () => {
  const user = vue.reactive({ sessionVersion: 1 })
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const queries: any[] = []
  let poll = () => {}
  let unmount = () => {}
  let cleared = false
  const doc = { hidden: true, addEventListener() {}, removeEventListener() {} }
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/common/apis/risk": { listRiskQueueApi: (query: unknown) => {
      queries.push(query)
      const request = Promise.withResolvers()
      pending.push(request)
      return request.promise
    } },
    "@@/utils/text": { formatTime: (value: string) => value }
  }, { ...globals, document: doc, setInterval: (callback: () => void) => {
    poll = callback
    return 1
  }, clearInterval: () => {
    cleared = true
  }, onMounted: (callback: () => void) => callback(), onBeforeUnmount: (callback: () => void) => {
    unmount = callback
  } })
  const page = load("@/pages/home/components/AiQueue.vue").default.setup({}, { expose() {}, emit() {} })
  assert.equal(queries.length, 1)
  poll()
  assert.equal(queries.length, 1)
  user.sessionVersion++
  await vue.nextTick()
  const current = page.load()
  pending[1].resolve({ total: 7, items: [] })
  await current
  pending[0].resolve({ total: 99, items: [] })
  await setImmediate()
  assert.equal(page.result.value.total, 7)
  page.state.value = "queued"
  await vue.nextTick()
  assert.equal(queries[2].state, "queued")
  page.stage.value = "analysis"
  await vue.nextTick()
  pending[2].resolve({ total: 999, items: [] })
  await setImmediate()
  assert.equal(page.result.value.total, 7)
  doc.hidden = false
  poll()
  pending[3].resolve({ total: 4, items: [] })
  await setImmediate()
  assert.equal(queries[3].stage, "analysis")
  assert.equal(page.result.value.total, 4)
  const late = page.load()
  unmount()
  pending[4].resolve({ total: 100, items: [] })
  await late
  assert.equal(page.result.value.total, 4)
  assert.equal(cleared, true)
})

test("回补确认绑定原预览，确认期间替换预览不能启动另一批次", async () => {
  const confirmation = Promise.withResolvers()
  const started: unknown[] = []
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => ({ sessionVersion: 1 }) },
    "@/common/apis/risk": { startReprocessApi: async (body: unknown) => {
      started.push(body)
    } },
    "@@/utils/text": { formatTime: (value: string) => value }
  }, { ...globals, ElMessageBox: { confirm: () => confirmation.promise } })
  const page = load("@/pages/home/components/EventResearch.vue").default.setup({}, { expose() {}, emit() {} })
  page.range.value = [new Date("2026-10-01"), new Date("2026-11-01")]
  await vue.nextTick()
  page.preview.value = { preview_id: "accepted-preview", articles: 2 }
  const action = page.start()
  page.preview.value = { preview_id: "replacement-preview", articles: 10 }
  confirmation.resolve(true)
  await action
  assert.deepEqual(started, [])
})

test("回补新闻明细随轮询和手动刷新更新，保留批次分页并支持失败重试", async () => {
  const user = vue.reactive({ sessionVersion: 1 })
  let state = "pending"
  let fail = false
  const requests: [number, number][] = []
  let poll = () => {}
  const doc = { hidden: false }
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/common/apis/risk": {
      listNewsEventsApi: async () => ({ items: [], total: 0 }),
      listReprocessApi: async () => [{ id: 7, state }],
      getReprocessDetailApi: async (id: number, page: number) => {
        requests.push([id, page])
        if (fail) throw new Error("Synthetic detail failure")
        return { id, state, total: 40, items: [{ id: 21, state }] }
      }
    },
    "@@/utils/text": { formatTime: (value: string) => value }
  }, { ...globals, document: doc, onMounted: (callback: () => void) => callback(), setInterval: (callback: () => void) => {
    poll = callback
    return 1
  } })
  const page = load("@/pages/home/components/EventResearch.vue").default.setup({}, { expose() {}, emit() {} })
  await setImmediate()
  await page.openBatch(7, 2)
  state = "done"
  poll()
  await setImmediate()
  assert.equal(page.batch.value.items[0].state, "done")
  assert.equal(page.batchPage.value, 2)
  assert.deepEqual(requests, [[7, 2], [7, 2]])
  doc.hidden = true
  poll()
  await setImmediate()
  assert.equal(requests.length, 2)
  fail = true
  await page.load()
  assert.equal(page.batchError.value, true)
  assert.equal(page.batch.value.items[0].state, "done")
  assert.equal(page.batchLoading.value, false)
  fail = false
  await page.openBatch(7, 2)
  assert.equal(page.batchError.value, false)
})

test("回补明细刷新不抢占新选择，旧会话与旧请求不能覆盖当前批次", async () => {
  const user = vue.reactive({ sessionVersion: 1 })
  const requests: { id: number, page: number, promise: ReturnType<typeof Promise.withResolvers> }[] = []
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/common/apis/risk": {
      listNewsEventsApi: async () => ({ items: [], total: 0 }),
      listReprocessApi: async () => [],
      getReprocessDetailApi: (id: number, page: number) => {
        const promise = Promise.withResolvers()
        requests.push({ id, page, promise })
        return promise.promise
      }
    },
    "@@/utils/text": { formatTime: (value: string) => value }
  }, globals)
  const page = load("@/pages/home/components/EventResearch.vue").default.setup({}, { expose() {}, emit() {} })
  const old = page.openBatch(7, 2)
  const selected = page.openBatch(8, 1)
  await page.load()
  assert.deepEqual(requests.map(r => [r.id, r.page]), [[7, 2], [8, 1]])
  requests[0].promise.resolve({ id: 7, items: [] })
  await old
  assert.equal(page.batchLoading.value, true)
  requests[1].promise.resolve({ id: 8, items: [] })
  await selected
  assert.equal(page.batch.value.id, 8)
  const late = page.openBatch(8, 2)
  user.sessionVersion++
  await vue.nextTick()
  const current = page.openBatch(9, 1)
  requests[2].promise.reject(new Error("Old session failure"))
  await late
  assert.equal(page.batchLoading.value, true)
  assert.equal(page.batchError.value, false)
  requests[3].promise.resolve({ id: 9, items: [] })
  await current
  assert.equal(page.batch.value.id, 9)
  assert.equal(page.batchId.value, 9)
  assert.equal(page.batchPage.value, 1)
})
function researchPage(api: Record<string, unknown> = {}, user = vue.reactive({ sessionVersion: 1 }), extra: Record<string, unknown> = {}, dependencies: Record<string, unknown> = {}) {
  const load = moduleLoader({
    "@@/apis/news-research": { getSettingsApi: async () => ({ config: {}, enabled: false }), listEventsApi: async () => ({ items: [], total: 0 }), listPackagesApi: async () => ({ items: [], total: 0 }), ...api },
    "@@/apis/skills": { listSkillTreeApi: async () => [] },
    "@@/apis/settings": { listProvidersApi: async () => [], listProviderModelsApi: async () => [] },
    "@/pinia/stores/user": { useUserStore: () => user },
    ...dependencies
  }, { ...globals, clearInterval, ...extra })
  return load("@/pages/home/index.vue").default.setup({}, { expose() {} })
}

test("两阶段首页保留新查询结果，旧会话与卸载后的响应不写回", async () => {
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const user = vue.reactive({ sessionVersion: 1 })
  let unmount = () => {}
  const page = researchPage({ listEventsApi: () => {
    const p = Promise.withResolvers()
    pending.push(p)
    return p.promise
  } }, user, { onBeforeUnmount: (fn: () => void) => unmount = fn })
  const first = page.load()
  const second = page.load()
  pending[1].resolve({ items: [{ id: 2 }], total: 1 })
  await second
  pending[0].resolve({ items: [{ id: 1 }], total: 1 })
  await first
  assert.equal(page.events.value[0].id, 2)
  const old = page.load()
  user.sessionVersion++
  await vue.nextTick()
  pending[2].resolve({ items: [{ id: 3 }], total: 1 })
  await old
  assert.equal(page.events.value.length, 0)
  unmount()
  await page.load()
  assert.equal(pending.length, 3)
})

for (const stage of ["index", "research"] as const) {
  test(`${stage} 模型列表保持手填值并拒绝旧配置与关闭后的响应`, async () => {
    const pending: ReturnType<typeof Promise.withResolvers>[] = []
    const page = researchPage({}, undefined, {}, { "@@/apis/settings": { listProvidersApi: async () => [], listProviderModelsApi: () => {
      const p = Promise.withResolvers()
      pending.push(p)
      return p.promise
    } } })
    page.config.value[stage].model = "manual"
    page.settingsOpened.value = true
    page.config.value[stage].provider_id = 1
    await vue.nextTick()
    page.config.value[stage].provider_id = 2
    await vue.nextTick()
    pending[1].resolve(["new"])
    await setImmediate()
    pending[0].resolve(["old"])
    await setImmediate()
    assert.deepEqual(Array.from(page.modelOptions.value[stage]), ["new"])
    assert.equal(page.config.value[stage].model, "manual")
    const closed = page.loadModels(stage)
    page.settingsOpened.value = false
    await vue.nextTick()
    pending[2].resolve(["closed"])
    await closed
    assert.equal(page.modelOptions.value[stage].length, 0)
  })
}

test("事件与包详情丢弃旧请求和关闭后响应，失败可重新读取", async () => {
  for (const kind of ["Event", "Package"]) {
    const pending: ReturnType<typeof Promise.withResolvers>[] = []
    const page = researchPage({ [`get${kind}Api`]: () => {
      const p = Promise.withResolvers()
      pending.push(p)
      return p.promise
    } })
    const first = page[`open${kind}`](1)
    const second = page[`open${kind}`](2)
    pending[1].resolve({ id: 2 })
    await second
    pending[0].resolve({ id: 1 })
    await first
    const ref = kind === "Event" ? page.detail : page.packageDetail
    assert.equal(ref.value.id, 2)
    const closed = page[`open${kind}`](3)
    ;(kind === "Event" ? page.opened : page.packageOpened).value = false
    await vue.nextTick()
    pending[2].resolve({ id: 3 })
    await closed
    assert.equal(ref.value, null)
    const failed = page[`open${kind}`](4)
    pending[3].reject(new Error("unavailable"))
    await failed
    assert.equal((kind === "Event" ? page.detailError : page.packageError).value, true)
    const retry = page[`open${kind}`](4)
    pending[4].resolve({ id: 4 })
    await retry
    assert.equal(ref.value.id, 4)
  }
})

test("两阶段保存各自思考强度，不提交第三阶段配置", async () => {
  let saved: any
  const page = researchPage({ saveSettingsApi: async (data: unknown) => {
    saved = JSON.parse(JSON.stringify(data))
  } })
  page.config.value.index.reasoning_effort = "low"
  page.config.value.research.reasoning_effort = "high"
  await page.save()
  assert.equal(saved.index.reasoning_effort, "low")
  assert.equal(saved.research.reasoning_effort, "high")
  assert.equal(Object.keys(saved).length, 3)
  page.config.value.research.reasoning_effort = null
  await page.save()
  assert.equal(saved.research.reasoning_effort, null)
  assert.equal(saved.index.reasoning_effort, "low")
})

test("Skill 草稿的旧会话响应不能替换当前列表或提示成功", async () => {
  const pending = Promise.withResolvers()
  const user = vue.reactive({ sessionVersion: 1 })
  let notices = 0
  const page = researchPage({ createSkillsApi: async () => ({ index: 1, research: 2 }) }, user, { ElMessage: { success() {
    notices++
  } } }, { "@@/apis/skills": { listSkillTreeApi: () => pending.promise } })
  const old = page.prepareSkills()
  await setImmediate()
  user.sessionVersion++
  await vue.nextTick()
  page.skills.value = [{ id: 9 }]
  pending.resolve([{ id: 1, scope: "news", kind: "skill" }])
  await old
  assert.equal(page.skills.value[0].id, 9)
  assert.equal(notices, 0)
})

test("两阶段操作拒绝重复提交和旧会话操作，失败保留配置草稿", async () => {
  const user = vue.reactive({ sessionVersion: 1 })
  const pending = Promise.withResolvers()
  let calls = 0
  const page = researchPage({ saveSettingsApi: () => {
    calls++
    return pending.promise
  } }, user)
  page.config.value.index.model = "draft"
  const first = page.save()
  await page.save()
  assert.equal(calls, 1)
  pending.reject(new Error("save failed"))
  await assert.rejects(first, /save failed/)
  assert.equal(page.config.value.index.model, "draft")
  assert.equal(page.busy.value, false)
  user.sessionVersion++
  await vue.nextTick()
  await page.save()
  assert.equal(calls, 1)
})

test("事件与新闻包分别分页查询，不调用第三阶段", async () => {
  const queries: string[] = []
  const page = researchPage({ listEventsApi: async () => {
    queries.push("events")
    return { items: [{ id: 1 }], total: 31 }
  }, listPackagesApi: async () => {
    queries.push("packages")
    return { items: [{ id: 2 }], total: 30 }
  } })
  await page.load()
  assert.equal(page.events.value[0].id, 1)
  page.tab.value = "packages"
  await vue.nextTick()
  await setImmediate()
  assert.equal(page.packages.value[0].id, 2)
  assert.equal(page.total.value, 30)
  assert.deepEqual(queries, ["events", "packages"])
})

for (const kind of ["Event", "Package"] as const) {
  test(`${kind} 重试后刷新详情，轮询恢复读取失败并丢弃关闭后的响应`, async () => {
    let state = "failed"
    let reads = 0
    let writes = 0
    let failRead = false
    const deferred = Promise.withResolvers()
    let delay = false
    const page = researchPage({
      [`get${kind}Api`]: async (id: number) => {
        reads++
        if (delay) return deferred.promise
        if (failRead) throw new Error("GET failed")
        return { id, state, error: state === "failed" ? "Old error" : null, versions: state === "done" ? [1] : [] }
      },
      [`retry${kind}Api`]: async () => {
        writes++
        state = "pending"
      }
    })
    const detail = kind === "Event" ? page.detail : page.packageDetail
    const opened = kind === "Event" ? page.opened : page.packageOpened
    const error = kind === "Event" ? page.detailError : page.packageError
    await page[`open${kind}`](7)
    await page[`retry${kind}`]()
    assert.equal(detail.value.state, "pending")
    assert.equal(detail.value.error, null)
    state = "done"
    failRead = true
    await page.refresh()
    assert.equal(detail.value.state, "pending")
    assert.equal(error.value, true)
    failRead = false
    await page.refresh()
    assert.equal(detail.value.state, "done")
    assert.equal(detail.value.versions.length, 1)
    assert.equal(error.value, false)
    delay = true
    const pending = page.refresh()
    await setImmediate()
    const count = reads
    await page.refresh()
    assert.equal(reads, count)
    opened.value = false
    await vue.nextTick()
    deferred.resolve({ id: 7, state: "failed" })
    await pending
    assert.equal(detail.value.state, "done")
    assert.equal(writes, 1)
  })
}

test("固定版本读取丢弃旧请求及旧会话响应", async () => {
  const first = Promise.withResolvers<any>()
  const second = Promise.withResolvers<any>()
  const user = vue.reactive({ sessionVersion: 1 })
  const page = researchPage({ getEventVersionApi: (_event: number, version: number) => version === 1 ? first.promise : second.promise }, user)
  const a = page.openVersion(8, 1)
  const b = page.openVersion(8, 2)
  second.resolve({ id: 2 })
  await b
  first.resolve({ id: 1 })
  await a
  assert.equal(page.versionDetail.value.id, 2)
  user.sessionVersion++
  await vue.nextTick()
  assert.equal(page.versionDetail.value, null)
  assert.equal(page.versionOpened.value, false)
})

test("词库读取不能打开新会话界面", async () => {
  const pending = Promise.withResolvers<any>()
  const user = vue.reactive({ sessionVersion: 1 })
  const page = researchPage({ getVocabularyApi: () => pending.promise }, user)
  const reading = page.openVocabulary()
  user.sessionVersion++
  await vue.nextTick()
  pending.resolve({ revision: 1, entries: [] })
  await reading
  assert.equal(page.vocabularyOpened.value, false)
})

test("词库读取保留最新草稿，关闭和卸载后不接收迟到响应", async () => {
  const pending: ReturnType<typeof Promise.withResolvers<any>>[] = []
  let unmount = () => {}
  const page = researchPage({ getVocabularyApi: () => {
    const value = Promise.withResolvers<any>()
    pending.push(value)
    return value.promise
  } }, undefined, { onBeforeUnmount: (fn: () => void) => unmount = fn })
  const first = page.openVocabulary()
  const second = page.openVocabulary()
  pending[1].resolve({ revision: 2, entries: [{ canonical: "新词", aliases: [] }] })
  await second
  page.vocabulary.value.entries[0].canonical = "编辑草稿"
  pending[0].resolve({ revision: 1, entries: [] })
  await first
  assert.equal(page.vocabulary.value.revision, 2)
  assert.equal(page.vocabulary.value.entries[0].canonical, "编辑草稿")
  const closed = page.openVocabulary()
  page.vocabularyOpened.value = false
  await vue.nextTick()
  pending[2].resolve({ revision: 3, entries: [] })
  await closed
  assert.equal(page.vocabularyOpened.value, false)
  const disposed = page.openVocabulary()
  unmount()
  pending[3].resolve({ revision: 4, entries: [] })
  await disposed
  assert.notEqual(page.vocabulary.value.revision, 4)
  await page.openVocabulary()
  assert.equal(pending.length, 4)
})

test("词库读取失败允许重试，未读取完成不能保存", async () => {
  const pending = Promise.withResolvers<any>()
  let reads = 0
  let writes = 0
  const page = researchPage({
    getVocabularyApi: () => {
      reads++
      if (reads === 1) return pending.promise
      return Promise.resolve({ revision: 2, entries: [] })
    },
    saveVocabularyApi: async () => {
      writes++
    }
  })
  const reading = page.openVocabulary()
  await page.saveVocabulary()
  assert.equal(writes, 0)
  pending.reject(new Error("unavailable"))
  await reading
  assert.equal(page.vocabularyError.value, true)
  assert.equal(page.vocabularyLoading.value, false)
  await page.saveVocabulary()
  assert.equal(writes, 0)
  await page.openVocabulary()
  assert.equal(page.vocabulary.value.revision, 2)
  assert.equal(page.vocabularyError.value, false)
})

test("词库保存拒绝重复提交，关闭后的响应不覆盖草稿，失败保留编辑", async () => {
  const pending = Promise.withResolvers<any>()
  let reads = 0
  let writes = 0
  const page = researchPage({
    getVocabularyApi: async () => ({ revision: ++reads, entries: [{ canonical: "词条", aliases: [] }] }),
    saveVocabularyApi: () => {
      writes++
      if (writes === 1) return pending.promise
      return Promise.reject(new Error("conflict"))
    }
  })
  await page.openVocabulary()
  const saving = page.saveVocabulary()
  await page.saveVocabulary()
  assert.equal(writes, 1)
  page.vocabularyOpened.value = false
  await vue.nextTick()
  await page.openVocabulary()
  assert.equal(reads, 1)
  page.vocabulary.value.entries[0].canonical = "保留草稿"
  pending.resolve({ revision: 2, entries: [] })
  await saving
  assert.equal(page.vocabularyOpened.value, false)
  assert.equal(page.vocabulary.value.entries[0].canonical, "保留草稿")
  assert.equal(page.vocabularyBusy.value, false)
  await page.openVocabulary()
  page.vocabulary.value.entries[0].canonical = "失败草稿"
  await assert.rejects(page.saveVocabulary(), /conflict/)
  assert.equal(page.vocabulary.value.entries[0].canonical, "失败草稿")
  assert.equal(page.vocabularyOpened.value, true)
  assert.equal(page.vocabularyBusy.value, false)
})
