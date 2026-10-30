import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import axios from "axios"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

const globals = { ...vue, onMounted() {}, onBeforeUnmount() {}, onBeforeRouteLeave() {}, ElMessage: { success() {}, warning() {} } }

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

test("风险首页丢弃旧窗口与旧会话结果，详情关闭后不写回", async () => {
  const requests: ReturnType<typeof Promise.withResolvers>[] = []
  const details = Promise.withResolvers()
  const user = { sessionVersion: 1 }
  const load = moduleLoader({
    "@/common/apis/risk": {
      getRiskOverviewApi: () => {
        const request = Promise.withResolvers()
        requests.push(request)
        return request.promise
      },
      listRiskEventsApi: async () => ({ items: [], total: 0 }),
      getRiskEventApi: () => details.promise
    },
    "@/common/apis/settings": {},
    "@/pinia/stores/user": { useUserStore: () => user }
  }, globals)
  const page = load("@/pages/home/index.vue").default.setup({}, { expose() {} })
  const old = page.load()
  const current = page.load()
  requests[1].resolve({ as_of: "current" })
  await current
  requests[0].resolve({ as_of: "old" })
  await old
  assert.equal(page.overview.value.as_of, "current")
  const previous = page.load()
  user.sessionVersion++
  requests[2].resolve({ as_of: "other-session" })
  await previous
  assert.equal(page.overview.value.as_of, "current")
  const opening = page.openEvent(1)
  await vue.nextTick()
  page.showDetail.value = false
  await vue.nextTick()
  details.resolve({ event: { id: 1 }, history: [] })
  await opening
  assert.equal(page.detail.value, null)
})

test("新闻模型列表随接入配置更新，保留手填值并丢弃旧配置、关闭及旧会话结果", async () => {
  const requests: { id: number, response: ReturnType<typeof Promise.withResolvers> }[] = []
  let unmount = () => {}
  const user = vue.reactive({ sessionVersion: 1 })
  const load = moduleLoader({
    "@/common/apis/risk": { getRiskSettingsApi: async () => ({ provider_id: 1, model: "manual-model", enabled: false }) },
    "@/common/apis/settings": {
      listProvidersApi: async () => [],
      listProviderModelsApi: (id: number) => {
        const response = Promise.withResolvers()
        requests.push({ id, response })
        return response.promise
      }
    },
    "@/pinia/stores/user": { useUserStore: () => user }
  }, { ...globals, clearInterval, onBeforeUnmount: (callback: () => void) => unmount = callback })
  const page = load("@/pages/home/index.vue").default.setup({}, { expose() {} })
  await page.openSettings()
  await vue.nextTick()
  assert.equal(requests[0].id, 1)
  page.settings.value.provider_id = 2
  await vue.nextTick()
  requests[1].response.resolve(["model-two"])
  await setImmediate()
  requests[0].response.resolve(["model-one"])
  await setImmediate()
  assert.deepEqual(Array.from(page.models.value), ["model-two"])
  assert.equal(page.settings.value.model, "manual-model")
  const failed = page.loadModels()
  requests[2].response.reject(new Error("目录不可用"))
  await failed
  assert.equal(page.modelsError.value, "目录不可用")
  assert.equal(page.settings.value.model, "manual-model")
  const closed = page.loadModels()
  page.showSettings.value = false
  await vue.nextTick()
  requests[3].response.resolve(["closed"])
  await closed
  assert.equal(page.models.value.length, 0)
  await page.openSettings()
  await vue.nextTick()
  user.sessionVersion++
  await vue.nextTick()
  requests[4].response.resolve(["previous-session"])
  await setImmediate()
  assert.equal(page.showSettings.value, false)
  assert.equal(page.models.value.length, 0)
  await page.openSettings()
  await vue.nextTick()
  unmount()
  requests[5].response.resolve(["unmounted"])
  await setImmediate()
  assert.equal(page.models.value.length, 0)
})

test("新闻 AI 详情绑定新闻修订版本，旧分析响应不能覆盖当前版本", async () => {
  const requests: { params: unknown, response: ReturnType<typeof Promise.withResolvers> }[] = []
  const props = vue.reactive({ newsId: 1, revision: 1 })
  const user = { sessionVersion: 1 }
  const load = moduleLoader({
    "@/common/apis/risk": { listRiskAnalysesApi: (params: unknown) => {
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
  assert.equal(page.analysis.value.id, 2)
  props.newsId = 3
  await vue.nextTick()
  user.sessionVersion++
  requests[2].response.resolve([{ id: 3 }])
  await setImmediate()
  assert.equal(page.analysis.value, null)
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
    "@/common/apis/risk": { listRiskAnalysesApi: async () => {
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
  assert.equal(page.analysis.value.state, "pending")
  document.hidden = true
  tick()
  assert.equal(requests, 1)
  document.hidden = false
  state = "done"
  tick()
  tick()
  await setImmediate()
  assert.equal(page.analysis.value.state, "done")
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
