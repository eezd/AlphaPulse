import assert from "node:assert/strict"
import test from "node:test"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

test("接入状态卸载后丢弃延迟响应", async () => {
  let unmount = () => {}
  const pending = Promise.withResolvers<unknown[]>()
  const load = moduleLoader({
    "@@/apis/settings": { listProviderAvailabilityApi: () => pending.promise },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ assertCurrent() {} }) }
  }, { ...vue, onMounted() {}, onBeforeUnmount(fn: () => void) {
    unmount = fn
  } })
  const page = load("@@/components/AiAvailability/index.vue").default.setup({}, { expose() {} })
  const request = page.refresh()
  unmount()
  pending.resolve([{ provider_id: 1, state: "waiting" }])
  await request
  assert.equal(page.states.value.length, 0)
})

test("接入状态只展示等待和试探，正常接入不显示故障", async () => {
  let unmount = () => {}
  const load = moduleLoader({
    "@@/apis/settings": { listProviderAvailabilityApi: async () => [
      { provider_id: 1, state: "normal" },
      { provider_id: 2, state: "waiting", retry_at: "2026-11-06T10:00:00Z" },
      { provider_id: 3, state: "probing", retry_at: "2026-11-06T10:00:00Z" }
    ] },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ assertCurrent() {} }) }
  }, { ...vue, onMounted() {}, onBeforeUnmount(fn: () => void) {
    unmount = fn
  } })
  const page = load("@@/components/AiAvailability/index.vue").default.setup({}, { expose() {} })
  try {
    await page.refresh()
    assert.equal(page.states.value.length, 2)
    assert.equal(page.states.value[0].state, "waiting")
    assert.equal(page.states.value[1].state, "probing")
    assert.equal(page.failed.value, false)
  } finally {
    unmount()
  }
})
