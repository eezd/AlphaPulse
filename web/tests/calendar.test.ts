import assert from "node:assert/strict"
import test from "node:test"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

function setup(api: () => Promise<any>) {
  let current = true
  const hooks: (() => void)[] = []
  const load = moduleLoader({
    "@@/apis/calendar": { listCalendarApi: api },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => current }) },
    "./components/CalendarDetail.vue": {}
  }, {
    ...vue,
    onMounted() {},
    onUnmounted: (fn: () => void) => hooks.push(fn),
    setInterval() {},
    setTimeout() {},
    clearInterval() {}
  })
  return {
    page: load("@/pages/calendar/index.vue").default.setup({}, { expose() {} }),
    invalidate: () => {
      current = false
    },
    unmount: () => hooks.forEach(hook => hook()),
    load
  }
}

const payload = {
  items: [
    { importance: 3, release_id: 1, kind: "data", country_code: "US", calendar_date: "2026-11-04", scheduled_at: "2026-11-04T13:15:00Z", time_precision: "minute", status: "scheduled" },
    { importance: 2, release_id: 2, kind: "event", country_code: "CN", calendar_date: "2026-11-04", scheduled_at: "2026-11-03T16:00:00Z", time_precision: "tentative", status: "recorded" },
    { importance: 1, release_id: 3, kind: "holiday", country_code: "JP", calendar_date: "2026-11-03", scheduled_at: "2026-11-02T16:00:00Z", time_precision: "day", status: "recorded" }
  ],
  countries: [],
  coverage: [],
  worker: null,
  jin10: null
}

test("calendar categories use local dates and do not count down unknown times", async () => {
  const { page } = setup(async () => payload)
  await page.load()
  assert.equal(page.groups.value[0][1][0].release_id, 1)
  page.kind.value = "event"
  assert.equal(page.groups.value[0][0], "2026-11-04")
  assert.equal(page.groups.value[0][1][0].release_id, 2)
  assert.equal(page.upcoming.value, undefined)
  page.kind.value = "holiday"
  assert.equal(page.groups.value[0][0], "2026-11-03")
  page.selectedCountries.value = ["US"]
  assert.equal(page.groups.value.length, 0)
})

test("calendar session changes and unmount discard late data", async () => {
  for (const action of ["invalidate", "unmount"] as const) {
    const pending = Promise.withResolvers<any>()
    const env = setup(() => pending.promise)
    const first = env.page.load()
    env[action]()
    pending.resolve(payload)
    await first
    assert.equal(env.page.data.value, null)
  }
})

test("calendar all categories, country selection and importance share the countdown filters", async () => {
  const { page } = setup(async () => payload)
  await page.load()
  page.kind.value = "all"
  page.now.value = Date.parse("2026-11-04T10:00:00Z")
  assert.equal(page.groups.value.flatMap(([, rows]: any) => rows).length, 3)
  assert.equal(page.upcoming.value.release_id, 1)
  page.selectedCountries.value = ["CN", "JP"]
  assert.equal(page.groups.value.flatMap(([, rows]: any) => rows).length, 2)
  assert.equal(page.upcoming.value, undefined)
  page.minimumImportance.value = 2
  assert.equal(page.groups.value.flatMap(([, rows]: any) => rows).length, 1)
  page.selectedCountries.value = ["US"]
  page.minimumImportance.value = 4
  assert.equal(page.groups.value.length, 0)
  assert.equal(page.upcoming.value, undefined)
})

test("calendar request order wins over late responses", async () => {
  const pending = Promise.withResolvers<any>()
  let calls = 0
  const { page } = setup(() => ++calls === 1 ? pending.promise : Promise.resolve(payload))
  const first = page.load()
  await page.load()
  pending.resolve({ ...payload, items: [] })
  await first
  assert.equal(page.data.value.items.length, 3)
})

test("calendar presentation preserves midnight, tentative time and local flag assets", () => {
  const { load } = setup(async () => payload)
  const presentation = load("@/pages/calendar/presentation")
  assert.equal(presentation.timeLabel({ scheduled_at: "2026-11-03T16:00:00Z", time_precision: "minute" }), "00:00")
  assert.equal(presentation.timeLabel({ time_precision: "tentative" }), "待定")
  assert.equal(presentation.timeLabel({ time_precision: "day" }), "全天")
  assert.equal(presentation.flagUrl({ country_code: "US", country_name: "美国" }), "/flags/us.svg")
  assert.equal(presentation.flagUrl({ country_code: "UNKNOWN", country_name: "未标注" }), null)
  assert.equal(presentation.value(null, "%"), "—")
  assert.equal(presentation.value("4.0-4.25", "%"), "4.0-4.25%")
})
