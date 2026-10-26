import assert from "node:assert/strict"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

interface Version {
  id: number
  content: string
  version_no: number
}

function setupComponent(component: string, modules: Record<string, unknown>, confirm: () => Promise<unknown> = async () => {}) {
  const globals = {
    ref: vue.ref,
    shallowRef: vue.shallowRef,
    reactive: vue.reactive,
    computed: vue.computed,
    watch: vue.watch,
    onMounted() {},
    onBeforeUnmount() {},
    onBeforeRouteLeave() {},
    setInterval() {},
    ElMessage: { success() {}, warning() {} },
    ElMessageBox: { confirm }
  }
  const load = moduleLoader({ "@/http/axios": { logRequestError() {} }, "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => true, assertCurrent() {} }) }, ...modules }, globals)
  return load(`@/pages/${component}`).default.setup({}, { expose() {} })
}

function skills(api: Record<string, unknown>, confirm?: () => Promise<unknown>) {
  return setupComponent("skills/index.vue", {
    "@@/apis/skills": api,
    "@@/components/SkillTree/index.vue": {},
    "@@/components/SkillVersionDrawer/index.vue": {},
    "@@/utils/markdown": { renderMarkdown: (text: string) => text }
  }, confirm)
}

async function checkSkillSwitch() {
  const a = Promise.withResolvers<Version[]>()
  const b = Promise.withResolvers<Version[]>()
  const drafts: { id: number, content: string }[] = []
  const nodes = [{ id: 1, kind: "skill" }, { id: 2, kind: "skill" }]
  const page = skills({
    listSkillTreeApi: async () => nodes,
    listSkillVersionsApi: (id: number) => id === 1 ? a.promise : b.promise,
    saveSkillDraftApi: async (id: number, content: string) => {
      drafts.push({ id, content })
      return { id: 20, version_no: 2, content }
    }
  })
  await page.loadTree()
  const first = page.select(nodes[0])
  const second = page.select(nodes[1])
  assert.equal(await page.saveDraft(), null)
  b.resolve([{ id: 2, content: "B 原文", version_no: 1 }])
  await second
  a.resolve([{ id: 1, content: "A 迟到", version_no: 1 }])
  await first
  assert.equal(page.content.value, "B 原文")
  page.content.value = "B 修改"
  await page.saveDraft()
  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].id, 2)
  assert.equal(drafts[0].content, "B 修改")
}

async function checkSkillReload() {
  const old = Promise.withResolvers<Version[]>()
  let calls = 0
  const page = skills({
    listSkillTreeApi: async () => [{ id: 1, kind: "skill" }],
    listSkillVersionsApi: () => ++calls === 1 ? old.promise : Promise.resolve([{ id: 2, content: "最新" }])
  })
  await page.loadTree()
  page.selectedId.value = 1
  const first = page.loadContent()
  await page.loadContent()
  old.resolve([{ id: 1, content: "旧内容", version_no: 1 }])
  await first
  assert.equal(page.content.value, "最新")
}

async function checkPublishSwitch() {
  const saved = Promise.withResolvers<Version>()
  const published: [number, number][] = []
  const nodes = [{ id: 1, kind: "skill" }, { id: 2, kind: "skill" }]
  const page = skills({
    listSkillTreeApi: async () => nodes,
    listSkillVersionsApi: async (id: number) => [{ id, content: `内容 ${id}` }],
    saveSkillDraftApi: () => saved.promise,
    publishSkillVersionApi: async (id: number, version: number) => published.push([id, version])
  })
  await page.loadTree()
  await page.select(nodes[0])
  page.content.value = "待发布修改"
  const publishing = page.publishLatest()
  await page.select(nodes[1])
  saved.resolve({ id: 10, version_no: 2, content: "待发布修改" })
  await publishing
  assert.deepEqual(published, [])
  assert.equal(page.content.value, "内容 2")
}

async function checkTickerSwitch() {
  const btc = Promise.withResolvers<Record<string, string>>()
  const eth = Promise.withResolvers<Record<string, string>>()
  const load = moduleLoader({
    "@/http/axios": { logRequestError() {} },
    "@@/apis/market": { getTickerApi: (_: string, symbol: string) => symbol === "BTCUSDT" ? btc.promise : eth.promise },
    "@@/utils/storage": { usePersisted: (_: string, value: unknown) => vue.ref(value) },
    "./chart": { DEFAULT_FAVORITE_INTERVALS: [], INTERVAL_LABELS: {}, MAIN_INDICATORS: [], SUB_INDICATORS: [] },
    "./useMarketSocket": { useMarketSocket: () => ({ connected: vue.ref(true), subscribe() {} }) },
    "./components/AddSymbolDialog.vue": {},
    "./components/SymbolManager.vue": {},
    "klinecharts": {}
  }, { ...vue, onMounted() {}, onBeforeUnmount() {}, setInterval() {} })
  const page = load("@/pages/market/composables/useMarketChart").useMarketChart()
  const first = page.loadTicker()
  page.selection.value = { market: "um", symbol: "ETHUSDT" }
  await vue.nextTick()
  eth.resolve({ lastPrice: "2000", highPrice: "2100", lowPrice: "1900", volume: "5", quoteVolume: "10000", priceChangePercent: "1" })
  await vue.nextTick()
  await new Promise(resolve => setImmediate(resolve))
  btc.resolve({ lastPrice: "80000", highPrice: "81000", lowPrice: "79000", volume: "1", quoteVolume: "80000", priceChangePercent: "2" })
  await first
  assert.equal(page.ticker.last, 2000)
  assert.equal(page.ticker.changePercent, 1)
}

async function checkPublishConfirmationSwitch() {
  const confirmation = Promise.withResolvers<void>()
  const published: [number, number][] = []
  const nodes = [{ id: 1, kind: "skill" }, { id: 2, kind: "skill" }]
  const page = skills({
    listSkillTreeApi: async () => nodes,
    listSkillVersionsApi: async (id: number) => [{ id, content: `内容 ${id}`, version_no: 1 }],
    publishSkillVersionApi: async (id: number, version: number) => published.push([id, version])
  }, () => confirmation.promise)
  await page.loadTree()
  await page.select(nodes[0])
  const publishing = page.publishLatest()
  await Promise.resolve()
  await page.select(nodes[1])
  confirmation.resolve()
  await publishing
  assert.deepEqual(published, [])
}

function contextEditor(api: Record<string, unknown>) {
  return setupComponent("chat/components/ContextTab.vue", {
    "@@/apis/chat": api,
    "@@/utils/markdown": { renderMarkdown: (text: string) => text },
    "@@/utils/text": { formatTime: (text: string) => text }
  })
}

async function checkContextRefresh() {
  const updated = Promise.withResolvers<{ id: number, content: string }>()
  const api = {
    getChatContextApi: async () => ({ id: 1, content: "原内容" }),
    listChatContextVersionsApi: async () => []
  }
  const page = contextEditor(api)
  await new Promise(resolve => setImmediate(resolve))
  api.getChatContextApi = () => updated.promise
  const refresh = page.refreshFromChat()
  page.content.value = "刷新期间的新输入"
  updated.resolve({ id: 2, content: "Chat 更新" })
  await refresh
  assert.equal(page.current.value.content, "Chat 更新")
  assert.equal(page.content.value, "刷新期间的新输入")
  assert.equal(page.dirty.value, true)
}

async function checkContextSave() {
  const saved = Promise.withResolvers<{ id: number, content: string }>()
  let submitted = ""
  const page = contextEditor({
    getChatContextApi: async () => ({ id: 1, content: "原内容" }),
    listChatContextVersionsApi: async () => [],
    saveChatContextApi: (content: string) => {
      submitted = content
      return saved.promise
    }
  })
  await new Promise(resolve => setImmediate(resolve))
  page.content.value = "提交的修改"
  const saving = page.save()
  page.content.value = "保存期间继续输入"
  saved.resolve({ id: 2, content: submitted })
  await saving
  assert.equal(submitted, "提交的修改")
  assert.equal(page.current.value.content, "提交的修改")
  assert.equal(page.content.value, "保存期间继续输入")
  assert.equal(page.dirty.value, true)
}

async function checkContextReloadAfterSave() {
  const stale = Promise.withResolvers<{ id: number, content: string }>()
  const api = {
    getChatContextApi: async () => ({ id: 1, content: "原内容" }),
    listChatContextVersionsApi: async () => [],
    saveChatContextApi: async (content: string) => ({ id: 2, content })
  }
  const page = contextEditor(api)
  await new Promise(resolve => setImmediate(resolve))
  api.getChatContextApi = () => stale.promise
  const loading = page.load()
  page.content.value = "已保存修改"
  await page.save()
  stale.resolve({ id: 1, content: "旧响应" })
  await loading
  assert.equal(page.current.value.id, 2)
  assert.equal(page.content.value, "已保存修改")
  assert.equal(page.dirty.value, false)
}

async function checkContextRefreshDuringSave() {
  const saved = Promise.withResolvers<{ id: number, content: string }>()
  const page = contextEditor({
    getChatContextApi: async () => ({ id: 1, content: "原内容" }),
    listChatContextVersionsApi: async () => [],
    saveChatContextApi: () => saved.promise
  })
  await new Promise(resolve => setImmediate(resolve))
  page.content.value = "提交的修改"
  const saving = page.save()
  await page.load()
  assert.equal(page.content.value, "提交的修改")
  saved.resolve({ id: 2, content: "提交的修改" })
  await saving
  assert.equal(page.dirty.value, false)
}

async function checkContextFailedSave() {
  const saved = Promise.withResolvers<{ id: number, content: string }>()
  const stale = Promise.withResolvers<{ id: number, content: string }>()
  const api = {
    getChatContextApi: async () => ({ id: 1, content: "原内容" }),
    listChatContextVersionsApi: async () => [],
    saveChatContextApi: () => saved.promise
  }
  const page = contextEditor(api)
  await new Promise(resolve => setImmediate(resolve))
  page.content.value = "保存失败后应保留的草稿"
  const saving = page.save()
  api.getChatContextApi = () => stale.promise
  const loading = page.load()
  saved.reject(new Error("保存失败"))
  await assert.rejects(saving, /保存失败/)
  stale.resolve({ id: 1, content: "原内容" })
  await loading
  assert.equal(page.content.value, "保存失败后应保留的草稿")
  assert.equal(page.dirty.value, true)
  assert.equal(page.saving.value, false)
}

await checkSkillSwitch()
await checkSkillReload()
await checkPublishSwitch()
await checkTickerSwitch()
await checkPublishConfirmationSwitch()
await checkContextRefresh()
await checkContextSave()
await checkContextReloadAfterSave()
await checkContextRefreshDuringSave()
await checkContextFailedSave()
console.log("页面异步竞态：10 项回归检查通过")
