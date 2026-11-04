import assert from "node:assert/strict"
import test from "node:test"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

test("event archives and fact packages use separate API contracts", async () => {
  const calls: { url: string }[] = []
  const load = moduleLoader({ "@/http/axios": { request: async (config: { url: string }) => calls.push(config) } })
  const api = load("@@/apis/risk")
  await api.listRiskEventsApi({ importance: 3, hours: 72 })
  await api.getNewsEventApi(9)
  await api.getRiskPackageApi(4)
  await api.listNewsPackagesApi(12, 2)
  assert.deepEqual(calls.map(c => c.url), ["/risk/events", "/risk/events/9", "/risk/packages/4", "/risk/news/12/packages"])
})

test("switching event details discards previous event and session responses", async () => {
  const first = Promise.withResolvers<any>()
  const second = Promise.withResolvers<any>()
  const user = vue.reactive({ sessionVersion: 1 })
  const load = moduleLoader({
    "@/common/apis/risk": { getNewsEventApi: (id: number) => id === 1 ? first.promise : second.promise },
    "@/pinia/stores/user": { useUserStore: () => user },
    "@@/utils/text": { formatTime: (s: string) => s }
  }, { ...vue, onMounted() {}, onBeforeUnmount() {}, setInterval() {}, clearInterval() {} })
  const page = load("@/pages/home/components/EventResearch.vue").default.setup({}, { expose() {} })
  const a = page.open(1)
  const b = page.open(2)
  second.resolve({ id: 2, title: "Second event", packages: [{ id: 4, fact: "Original fact" }] })
  await b
  first.resolve({ id: 1, title: "Old event" })
  await a
  assert.equal(page.detail.value.id, 2)
  assert.equal(page.detail.value.packages[0].fact, "Original fact")
  const pending = page.open(1)
  user.sessionVersion++
  await vue.nextTick()
  await pending
  assert.equal(page.detail.value, null)
  assert.equal(page.opened.value, false)
})
