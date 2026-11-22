import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import axios from "axios"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

const globals = { ...vue, useRoute: () => ({ query: {} }), onMounted() {}, onBeforeUnmount() {}, onBeforeRouteLeave() {}, ElMessage: { success() {}, warning() {} } }

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

test("新闻来源计数只接受最新响应，读取失败不伪装为零", async () => {
  const pending: ReturnType<typeof Promise.withResolvers>[] = []
  const notifications: boolean[] = []
  let active = true
  const load = moduleLoader({
    "@@/apis/crawler": { listSourcesApi: (notify: boolean) => {
      notifications.push(notify)
      const request = Promise.withResolvers()
      pending.push(request)
      return request.promise
    } },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => active }) },
    "@@/utils/text": { formatTime: (value: string) => value },
    "@element-plus/icons-vue": { Refresh: {}, Search: {} },
    "./components/NewsDetail.vue": { default: {} },
    "./composables/useNewsFeed": { useNewsFeed: () => ({}) }
  }, globals)
  const page = load("@/pages/news/index.vue").default.setup({}, { expose() {} })
  const old = page.refreshSourceCounts()
  const current = page.refreshSourceCounts()
  pending[1].resolve({ sources: [{ code: "citi_insights", total_items: 12, today_new: 0 }] })
  await current
  pending[0].resolve({ sources: [{ code: "citi_insights", total_items: 1, today_new: 1 }] })
  await old
  assert.equal(page.sourceCounts.value.citi_insights.total_items, 12)
  assert.equal(page.sourceCounts.value.citi_insights.today_new, 0)
  const failing = page.refreshSourceCounts()
  pending[2].reject(new Error("读取失败"))
  await failing
  assert.deepEqual(JSON.parse(JSON.stringify(page.sourceCounts.value)), {})
  const stale = page.refreshSourceCounts()
  active = false
  pending[3].resolve({ sources: [{ code: "citi_insights", total_items: 99, today_new: 9 }] })
  await stale
  assert.deepEqual(notifications, [false, false, false, false])
  assert.deepEqual(JSON.parse(JSON.stringify(page.sourceCounts.value)), {})
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
  pending[0].resolve({ items: [{ id: 1, revision_no: 1 }, { id: 2, revision_no: 1, analysis: { state: "processing" } }], total: 120 })
  await initial
  page.pendingCount.value = 7
  page.openedId.value = 1
  const refreshing = page.refreshRisk()
  assert.deepEqual(JSON.parse(JSON.stringify(queries[1])), { ids: [1, 2], limit: 100 })
  pending[1].resolve({ items: [{ id: 1, revision_no: 1, analysis: { state: "done", importance_stars: 5 } }, { id: 2, revision_no: 2, analysis: { state: "done", importance_stars: 1 } }], total: 2 })
  await refreshing
  assert.equal(page.items.value[0].analysis.importance_stars, 5)
  assert.equal(page.items.value[1].analysis.state, "processing")
  assert.equal(page.total.value, 120)
  assert.equal(page.pendingCount.value, 7)
  assert.equal(page.openedId.value, 1)
  assert.equal(scrolls, 1)
  const old = page.refreshRisk()
  const current = page.load()
  pending[3].resolve({ items: [{ id: 3, revision_no: 1 }], total: 1 })
  await current
  pending[2].resolve({ items: [{ id: 3, revision_no: 1, analysis: { state: "done" } }], total: 1 })
  await old
  assert.equal(page.items.value[0].analysis, undefined)
  const previous = page.refreshRisk()
  active = false
  pending[4].resolve({ items: [{ id: 3, revision_no: 1, analysis: { state: "done" } }], total: 1 })
  await previous
  assert.equal(page.items.value[0].analysis, undefined)
  await page.refreshRisk()
  assert.equal(queries.length, 5)
})
