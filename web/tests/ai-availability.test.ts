import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

test("接入状态卸载后丢弃延迟响应", async () => {
  let unmount = () => {}
  const pending = Promise.withResolvers<unknown[]>()
  const load = moduleLoader({
    "@@/apis/settings": { listProviderAvailabilityApi: () => pending.promise },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => true }) }
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

test("接入状态汇总只展示等待，提示其他请求独立执行", async () => {
  let unmount = () => {}
  const load = moduleLoader({
    "@@/apis/settings": { listProviderAvailabilityApi: async () => [
      { provider_id: 1, state: "normal" },
      { provider_id: 2, state: "waiting", retry_at: "2026-11-06T10:00:00Z" }
    ] },
    "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => true }) }
  }, { ...vue, onMounted() {}, onBeforeUnmount(fn: () => void) {
    unmount = fn
  } })
  const page = load("@@/components/AiAvailability/index.vue").default.setup({}, { expose() {} })
  try {
    await page.refresh()
    assert.equal(page.states.value.length, 1)
    assert.equal(page.states.value[0].state, "waiting")
    assert.equal(page.failed.value, false)
    const source = readFileSync(new URL("../src/common/components/AiAvailability/index.vue", import.meta.url), "utf8")
    assert.match(source, /部分请求正在等待重试；其他请求独立执行/)
    assert.match(source, /最早下次尝试/)
    assert.doesNotMatch(source, /probing|单请求试探/)
  } finally {
    unmount()
  }
})

for (const outcome of ["success", "failure"]) {
  test(`接入状态会话切换后丢弃旧${outcome}响应`, async () => {
    let current = true
    let unmount = () => {}
    const pending = Promise.withResolvers<unknown[]>()
    const load = moduleLoader({
      "@@/apis/settings": { listProviderAvailabilityApi: () => pending.promise },
      "@@/composables/useSessionScope": { useSessionScope: () => ({ isCurrent: () => current }) }
    }, { ...vue, onMounted() {}, onBeforeUnmount(fn: () => void) {
      unmount = fn
    } })
    const page = load("@@/components/AiAvailability/index.vue").default.setup({}, { expose() {} })
    try {
      const request = page.refresh()
      current = false
      if (outcome === "success") pending.resolve([{ provider_id: 1, state: "waiting" }])
      else pending.reject(new Error("old session response"))
      await request
      assert.equal(page.states.value.length, 0)
      assert.equal(page.failed.value, false)
    } finally {
      unmount()
    }
  })
}
