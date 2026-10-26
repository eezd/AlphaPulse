import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

const globals = { ...vue, onMounted() {}, onBeforeUnmount() {}, onBeforeRouteLeave() {}, ElMessage: { success() {}, warning() {} } }

const tradingStatus = (limit = 120) => ({ exchange: "bybit", configured: true, provider_id: null, reasoning_effort: null, snapshot: null, today: { analyses: 10, limit, input_tokens: 0, output_tokens: 0 } })

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
