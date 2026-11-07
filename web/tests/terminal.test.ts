import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

test("CSV preserves Unicode, separators and numbers while escaping spreadsheet formulas", async () => {
  let downloaded: Blob | undefined
  let clicked = false
  const load = moduleLoader({}, {
    Blob,
    URL: { createObjectURL: (blob: Blob) => {
      downloaded = blob
      return "blob:export"
    }, revokeObjectURL() {} },
    document: { createElement: () => ({ click() {
      clicked = true
    } }) },
    setTimeout() {}
  })
  load("@@/utils/export").downloadCsv("news.csv", [["中文,标题", "引用\"原文\"", "\n=IMPORTXML(url)", -1.5, null]])
  assert.equal(clicked, true)
  assert.equal(await downloaded!.text(), "\"中文,标题\",\"引用\"\"原文\"\"\",\"'\n=IMPORTXML(url)\",\"-1.5\",\"\"")
})

test("quote snapshots reject old data; trades deduplicate and sort; changing symbol clears state", async () => {
  let receive: (message: any) => void = () => {}
  const subscriptions: unknown[] = []
  const props = vue.reactive({ market: "um", symbol: "BTCUSDT" })
  const load = moduleLoader({
    "../composables/useMarketSocket": { useMarketSocket: (_k: unknown, _t: unknown, quote: typeof receive) => {
      receive = quote
      return { connected: vue.ref(true), subscribe: (sub: unknown) => subscriptions.push(sub) }
    } }
  }, { ...vue, setInterval() {}, clearInterval() {}, onBeforeUnmount() {} })
  const panel = load("@/pages/market/components/QuotePanel.vue").default.setup(props, { expose() {} })
  receive({ type: "depth", data: { at: 200, bids: [["2", "1"]] } })
  receive({ type: "depth", data: { at: 100, bids: [["1", "1"]] } })
  assert.equal(panel.book.value.bids[0][0], "2")
  receive({ type: "mark_price", data: { at: 200, mark_price: "3" } })
  receive({ type: "mark_price", data: { at: 100, mark_price: "2" } })
  assert.equal(panel.mark.value.mark_price, "3")
  for (const id of [3, 1, 2, 3]) receive({ type: "trade", data: { id, time: id * 10 } })
  assert.deepEqual(Array.from(panel.trades.value, (r: any) => r.id), [3, 2, 1])
  panel.now.value = 16000
  assert.equal(panel.stale.value, true)
  assert.equal(panel.markStale.value, true)
  props.symbol = "ETHUSDT"
  await vue.nextTick()
  assert.equal(panel.book.value.at, undefined)
  assert.equal(panel.trades.value.length, 0)
  assert.equal((subscriptions.at(-1) as any).symbol, "ETHUSDT")
})

const globals = { ...vue, onMounted() {}, onBeforeUnmount() {}, onBeforeRouteLeave() {}, useRoute: () => ({ query: {} }), setInterval() {}, clearInterval() {}, ElMessage: { warning() {} } }

test("market fullscreen includes body popups and can exit fullscreen", async () => {
  const calls: string[] = []
  const document = {
    fullscreenElement: null as object | null,
    documentElement: { async requestFullscreen() {
      calls.push("enter")
      document.fullscreenElement = document.documentElement
    } },
    async exitFullscreen() {
      calls.push("exit")
      document.fullscreenElement = null
    }
  }
  const load = moduleLoader({
    "./composables/useMarketChart": { useMarketChart: () => ({}) },
    "./components/AddSymbolDialog.vue": {},
    "./components/QuotePanel.vue": {},
    "./components/SymbolManager.vue": {}
  }, { ...globals, document })
  const page = load("@/pages/market/index.vue").default.setup({}, { expose() {} })
  await page.fullscreen()
  assert.equal(document.fullscreenElement, document.documentElement)
  await page.fullscreen()
  assert.deepEqual(calls, ["enter", "exit"])
})

for (const refresh of ["load", "refreshRisk"] as const) {
  for (const responseFirst of ["list", "bookmark"]) {
    test(`bookmark updates survive ${refresh} when ${responseFirst} responds first`, async () => {
      const requests: ReturnType<typeof Promise.withResolvers<any>>[] = []
      const saving = Promise.withResolvers<void>()
      let writes = 0
      const load = moduleLoader({
        "@@/apis/news": {
          listNewsApi: () => {
            const request = Promise.withResolvers<any>()
            requests.push(request)
            return request.promise
          },
          bookmarkNewsApi: () => {
            writes++
            return saving.promise
          }
        },
        "@@/composables/useSourceNames": { useSourceNames: () => vue.ref({}) },
        "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => true }) },
        "@@/utils/storage": { usePersisted: (_name: string, value: unknown) => vue.ref(value) },
        "@/http/useEventSource": { useEventSource: () => ({ connected: vue.ref(true) }) }
      }, globals)
      const feed = load("@/pages/news/composables/useNewsFeed").useNewsFeed()
      feed.filters.processing_state = "done"
      feed.items.value = [{ id: 1, bookmarked: false }]
      const original = feed.items.value[0]
      const action = feed.toggleBookmark(original)
      await feed.toggleBookmark(original)
      assert.equal(writes, 1)
      const reading = feed[refresh]()
      const old = { items: [{ id: 1, bookmarked: false }], total: 1 }
      if (responseFirst === "list") {
        requests[0].resolve(old)
        await reading
        assert.notEqual(feed.items.value[0], original)
        saving.resolve()
        await action
      } else {
        saving.resolve()
        await setImmediate()
        assert.equal(requests.length, 2)
        assert.equal(feed.items.value[0].bookmarked, true)
        requests[1].resolve({ items: [{ id: 1, bookmarked: true }], total: 1 })
        await action
        requests[0].resolve(old)
        await reading
      }
      assert.equal(feed.items.value[0].bookmarked, true)
      assert.equal(feed.savingBookmarks.size, 0)
    })
  }
}

test("bookmark failures preserve state, old sessions are ignored, and removing a filtered bookmark reloads", async () => {
  let current = true
  let saving = Promise.withResolvers<void>()
  const reading = Promise.withResolvers<any>()
  let reads = 0
  const writes: unknown[] = []
  const load = moduleLoader({
    "@@/apis/news": {
      listNewsApi: () => {
        reads++
        return reading.promise
      },
      bookmarkNewsApi: (...args: unknown[]) => {
        writes.push(args)
        return saving.promise
      }
    },
    "@@/composables/useSourceNames": { useSourceNames: () => vue.ref({}) },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => current }) },
    "@@/utils/storage": { usePersisted: (_name: string, value: unknown) => vue.ref(value) },
    "@/http/useEventSource": { useEventSource: () => ({ connected: vue.ref(true) }) }
  }, globals)
  const feed = load("@/pages/news/composables/useNewsFeed").useNewsFeed()
  feed.items.value = [{ id: 1, bookmarked: true }]
  feed.filters.bookmarked = true
  const failed = feed.toggleBookmark(feed.items.value[0])
  saving.reject(new Error("写入失败"))
  await assert.rejects(failed, /写入失败/)
  assert.equal(feed.items.value[0].bookmarked, true)
  assert.equal(feed.savingBookmarks.size, 0)
  assert.equal(reads, 0)
  saving = Promise.withResolvers<void>()
  const obsolete = feed.toggleBookmark(feed.items.value[0])
  current = false
  saving.resolve()
  await obsolete
  assert.equal(feed.items.value[0].bookmarked, true)
  assert.equal(reads, 0)
  current = true
  const removed = feed.toggleBookmark(feed.items.value[0])
  await setImmediate()
  assert.equal(reads, 1)
  reading.resolve({ items: [], total: 0 })
  await removed
  assert.deepEqual(writes, [[1, false], [1, false], [1, false]])
  assert.equal(feed.items.value.length, 0)
  assert.equal(feed.total.value, 0)
})

test("research export binds conclusions and original sources to the successful version", async () => {
  const request = Promise.withResolvers<any>()
  const downloads: string[] = []
  const calls: unknown[] = []
  let fail = false
  const load = moduleLoader({
    "@@/apis/news-research": { getEventVersionApi: (...args: unknown[]) => {
      calls.push(args)
      if (fail) throw new Error("版本读取失败")
      return request.promise
    } },
    "@@/apis/settings": {},
    "@@/apis/skills": {},
    "@@/utils/export": { downloadText: (_name: string, content: string) => downloads.push(content) },
    "@/pinia/stores/user": { useUserStore: () => vue.reactive({ sessionVersion: 1 }) }
  }, globals)
  const page = load("@/pages/home/index.vue").default.setup({}, { expose() {} })
  const result = { title: "已核验事件", summary: "已核验结论", importance_stars: 4, risk_stars: 3, confidence: "中", facts: "事实", reasoning: "判断", verification: "核验", asset_impacts: "影响", opinions: "意见", counter_evidence: "反证", uncertainty: "待确认", watchpoints: "观察", timeline_points: [] }
  page.opened.value = true
  page.detail.value = { id: 1, title: "当前事件", revision: 2, state: "pending", result: { ...result, summary: "不能使用当前缓存结论" }, packages: [{ id: 1, news_id: 1, title: "已修订标题", url: "https://example.invalid/revised" }, { id: 2, news_id: 2, title: "新报道", url: "https://example.invalid/new" }], versions: [{ id: 11, revision: 2, state: "failed", result: null }, { id: 10, revision: 1, state: "done", result }] }
  const exporting = page.exportReport()
  await page.exportReport()
  assert.deepEqual(calls, [[1, 10]])
  request.resolve({ id: 10, revision: 1, result, packages: [{ id: 1, news_id: 1 }], articles: [{ id: 1, title: "研究时原始标题", url: "https://example.invalid/original" }] })
  await exporting
  const report = downloads[0]
  assert.match(report, /成功研究版本 #10 · 研究修订 1/)
  assert.match(report, /当前事件修订 2/)
  assert.match(report, /新增或修订材料尚未由本报告核验/)
  assert.match(report, /已核验结论/)
  assert.doesNotMatch(report, /不能使用当前缓存结论|已修订标题/)
  const [included, additional] = report.split("## 当前事件其他新闻包（未纳入本版研究）")
  assert.match(included, /研究时原始标题/)
  assert.doesNotMatch(included, /新报道/)
  assert.match(additional, /新报道/)
  assert.equal(page.exportingReport.value, false)

  const closed = page.exportReport()
  page.opened.value = false
  await closed
  assert.equal(downloads.length, 1)
  page.opened.value = true
  page.detail.value.versions = []
  await page.exportReport()
  assert.equal(calls.length, 2)
  page.detail.value.versions = [{ id: 10, revision: 1, state: "done", result }]
  fail = true
  await assert.rejects(page.exportReport(), /版本读取失败/)
  assert.equal(page.exportingReport.value, false)
  assert.equal(downloads.length, 1)
})

test("status-filter polling replaces rows and totals, continues on empty/done lists and rejects obsolete results", async () => {
  const pending: ReturnType<typeof Promise.withResolvers<any>>[] = []
  const queries: any[] = []
  const mounted: (() => void)[] = []
  let tick = () => {}
  const load = moduleLoader({
    "@@/apis/news": { listNewsApi: (query: unknown) => {
      queries.push(query)
      const response = Promise.withResolvers<any>()
      pending.push(response)
      return response.promise
    } },
    "@@/composables/useSourceNames": { useSourceNames: () => vue.ref({}) },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => true }) },
    "@@/utils/storage": { usePersisted: (_name: string, value: unknown) => vue.ref(value) },
    "@/http/useEventSource": { useEventSource: () => ({ connected: vue.ref(true) }) }
  }, { ...globals, document: { hidden: false }, onMounted: (callback: () => void) => mounted.push(callback), setInterval: (callback: () => void) => tick = callback })
  const feed = load("@/pages/news/composables/useNewsFeed").useNewsFeed()
  feed.filters.processing_state = "index_processing"
  feed.filters.q = "流动性"
  feed.filters.bookmarked = true
  feed.filters.sort = "asc"
  feed.items.value = [{ id: 1, revision_no: 1, risk: { state: "index_processing" } }]
  feed.total.value = 1
  const refresh = feed.refreshRisk()
  assert.equal(queries[0].processing_state, "index_processing")
  assert.equal(queries[0].q, "流动性")
  assert.equal(queries[0].bookmarked, true)
  assert.equal(queries[0].sort, "asc")
  assert.equal(queries[0].ids, undefined)
  pending[0].resolve({ items: [], total: 0 })
  await refresh
  assert.equal(feed.items.value.length, 0)
  assert.equal(feed.total.value, 0)
  mounted[1]()
  tick()
  pending[1].resolve({ items: [{ id: 2, revision_no: 1, risk: { state: "index_processing" } }], total: 1 })
  await setImmediate()
  assert.equal(feed.items.value[0].id, 2)
  feed.filters.processing_state = "done"
  const current = feed.load()
  pending[2].resolve({ items: [{ id: 3, revision_no: 1, risk: { state: "done" } }], total: 1 })
  await current
  tick()
  const newer = feed.load()
  pending[4].resolve({ items: [{ id: 4, revision_no: 1, risk: { state: "done" } }], total: 1 })
  await newer
  pending[3].resolve({ items: [{ id: 99 }], total: 99 })
  await setImmediate()
  assert.equal(feed.items.value[0].id, 4)
  assert.equal(feed.total.value, 1)

  feed.page.value = 2
  await vue.nextTick()
  pending[5].resolve({ items: [{ id: 5 }], total: 31 })
  await setImmediate()
  const shrinking = feed.refreshRisk()
  pending[6].resolve({ items: [], total: 1 })
  await shrinking
  await vue.nextTick()
  assert.equal(feed.page.value, 1)
  pending[7].resolve({ items: [{ id: 6 }], total: 1 })
  await setImmediate()
  assert.equal(feed.items.value[0].id, 6)
})

test("Skill search disables drag writes and clearing search restores ordering", async () => {
  const writes: unknown[] = []
  const load = moduleLoader({
    "@@/apis/skills": { moveSkillNodeApi: async (...args: unknown[]) => writes.push(args) },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ assertCurrent() {} }) }
  }, globals)
  const nodes = [1, 2, 3].map(id => ({ id, name: `Skill ${id}`, parent_id: null, kind: "skill" }))
  const props = vue.reactive({ nodes: nodes.slice(1), editable: true, draggable: false })
  const tree = load("@@/components/SkillTree/index.vue").default.setup(props, { expose() {}, emit() {} })
  const dragged = { data: { data: nodes[1] } }
  const target = { data: { data: nodes[2] } }
  assert.equal(tree.canDrag.value, false)
  assert.equal(tree.allowDrop(dragged, target, "next"), false)
  await tree.onDrop(dragged, target, "after")
  assert.equal(writes.length, 0)
  props.nodes = nodes
  props.draggable = true
  assert.equal(tree.canDrag.value, true)
  await tree.onDrop(dragged, target, "after")
  assert.deepEqual(writes, [[2, null, 2]])
})
