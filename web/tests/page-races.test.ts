import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire, Module } from "node:module"
import { runInNewContext } from "node:vm"
import ts from "typescript"
import * as vue from "vue"
import { compileScript, parse } from "vue/compiler-sfc"

const require = createRequire(import.meta.url)

interface Version {
  id: number
  content: string
  version_no: number
}

function setupPage(page: string, modules: Record<string, unknown>, confirm: () => Promise<unknown> = async () => {}) {
  const filename = new URL(`../src/pages/${page}/index.vue`, import.meta.url)
  const { descriptor } = parse(readFileSync(filename, "utf8"))
  const script = compileScript(descriptor, { id: page }).content
  const code = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const globals = {
    ref: vue.ref,
    reactive: vue.reactive,
    computed: vue.computed,
    watch: vue.watch,
    onMounted() {},
    onBeforeUnmount() {},
    onBeforeRouteLeave() {},
    setInterval() {},
    ElMessage: { success() {} },
    ElMessageBox: { confirm }
  }
  const mod = new Module(filename.pathname)
  const load = (path: string) => path in modules ? modules[path] : require(path)
  runInNewContext(code, { require: load, module: mod, exports: mod.exports, ...globals }, { filename: `${page}.vue` })
  return mod.exports.default.setup({}, { expose() {} })
}

function skills(api: Record<string, unknown>, confirm?: () => Promise<unknown>) {
  return setupPage("skills", {
    "@/apis/skills": api,
    "@/components/SkillTree.vue": {},
    "@/components/SkillVersionDrawer.vue": {},
    "@/utils/markdown": { renderMarkdown: (text: string) => text }
  }, confirm)
}

async function checkSkillSwitch() {
  const a = Promise.withResolvers<Version[]>()
  const b = Promise.withResolvers<Version[]>()
  const drafts: { id: number, content: string }[] = []
  const nodes = [{ id: 1, kind: "skill" }, { id: 2, kind: "skill" }]
  const page = skills({
    listSkillTree: async () => nodes,
    listSkillVersions: (id: number) => id === 1 ? a.promise : b.promise,
    saveSkillDraft: async (id: number, content: string) => {
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
    listSkillTree: async () => [{ id: 1, kind: "skill" }],
    listSkillVersions: () => ++calls === 1 ? old.promise : Promise.resolve([{ id: 2, content: "最新" }])
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
    listSkillTree: async () => nodes,
    listSkillVersions: async (id: number) => [{ id, content: `内容 ${id}` }],
    saveSkillDraft: () => saved.promise,
    publishSkillVersion: async (id: number, version: number) => published.push([id, version])
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
  const page = setupPage("market", {
    "@/apis/market": { getTicker: (_: string, symbol: string) => symbol === "BTCUSDT" ? btc.promise : eth.promise },
    "@/utils/storage": { usePersisted: (_: string, value: unknown) => vue.ref(value) },
    "./chart": { DEFAULT_FAVORITE_INTERVALS: [], INTERVAL_LABELS: {}, MAIN_INDICATORS: [], SUB_INDICATORS: [] },
    "./useMarketSocket": { useMarketSocket: () => ({ connected: vue.ref(true), subscribe() {} }) },
    "./components/AddSymbolDialog.vue": {},
    "./components/SymbolManager.vue": {},
    "klinecharts": {}
  })
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
    listSkillTree: async () => nodes,
    listSkillVersions: async (id: number) => [{ id, content: `内容 ${id}`, version_no: 1 }],
    publishSkillVersion: async (id: number, version: number) => published.push([id, version])
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

await checkSkillSwitch()
await checkSkillReload()
await checkPublishSwitch()
await checkTickerSwitch()
await checkPublishConfirmationSwitch()
console.log("页面异步竞态：5 项回归检查通过")
