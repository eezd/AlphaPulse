import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import { moduleLoader, require } from "./module-loader.ts"

const axios = require("axios").default
const pinia = require("pinia")
const vue = require("vue")
const { createRouter, createMemoryHistory } = require("vue-router")

const ok = (data: unknown) => ({ status: 200, body: { code: 0, data, message: "" } })
const failure = (status: number) => ({ status, body: { code: status, data: null, message: `failure ${status}` } })

function environment(respond: (config: any) => any) {
  const messages: string[] = []
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) }
  pinia.setActivePinia(pinia.createPinia())
  const routes = [
    { path: "/login", name: "login" },
    { path: "/news", name: "news", meta: { admin: true } },
    { path: "/home", name: "home", meta: { admin: true } },
    { path: "/calendar", name: "calendar", meta: { admin: true } },
    { path: "/403", name: "forbidden" },
    { path: "/connection-error", name: "connection-error", meta: { public: true } }
  ].map(route => ({ ...route, component: { render: () => null } }))
  const router = createRouter({ history: createMemoryHistory(), routes })
  const load = moduleLoader({
    "@/router": { router },
    "element-plus": { ElMessage: { error: (message: string) => messages.push(message) } },
    "axios": { __esModule: true, ...axios, default: { ...axios, create: (config: any) => axios.create({
      ...config,
      adapter: async (config: any) => {
        const { status, body } = await respond(config)
        const response = { status, data: body, statusText: "", headers: {}, config }
        if (status >= 400) throw new axios.AxiosError(`HTTP ${status}`, "ERR_BAD_RESPONSE", config, undefined, response)
        return response
      }
    }) } }
  }, { localStorage: storage, ...vue })
  const { request, ApiError } = load("@/http/axios")
  const user = load("@/pinia/stores/user").useUserStore()
  const guards = load("@/router/guards")
  return { request, ApiError, user, guards, router, messages, load, storage }
}

test("JSON 解包、明确拒绝错误、403 保留身份且只提示一次", async () => {
  let result = ok([1])
  const env = environment(() => result)
  env.user.setToken("current")
  assert.deepEqual(await env.request({ url: "/news", method: "get" }), [1])
  result = failure(403)
  await assert.rejects(env.request({ url: "/news", method: "get" }), (error: any) => error.code === 403)
  assert.equal(env.user.token, "current")
  assert.equal(env.messages.length, 1)
  result = { status: 200, body: { code: 400, data: null, message: "business failure" } }
  await assert.rejects(env.request({ url: "/news", method: "get" }), /business failure/)
  result = { status: 200, body: { items: [] } } as any
  await assert.rejects(env.request({ url: "/news", method: "get" }), /响应格式无效/)
})

test("旧身份恢复响应不能写入新会话；同 Token 重新设置也隔离", async () => {
  const pending = Promise.withResolvers<any>()
  const env = environment(() => pending.promise)
  env.user.setToken("same-token")
  const restore = env.user.fetchMe()
  const rejected = assert.rejects(restore, (error: any) => axios.isCancel(error))
  await setImmediate()
  env.user.setToken("same-token")
  pending.resolve(ok({ username: "old", is_admin: true }))
  await rejected
  assert.equal(env.user.identity, undefined)
  assert.equal(env.user.token, "same-token")
  assert.equal(env.messages.length, 0)
})

test("旧 401 与旧注销结果都不能注销新登录", async () => {
  for (const request of ["read", "logout"]) {
    const pending = Promise.withResolvers<any>()
    const env = environment(() => pending.promise)
    env.user.setToken("old")
    const operation = request === "logout" ? env.user.logout() : env.request({ url: "/news", method: "get" })
    const rejected = assert.rejects(operation, (error: any) => axios.isCancel(error))
    await setImmediate()
    env.user.setToken("new")
    pending.resolve(request === "logout" ? ok(null) : failure(401))
    await rejected
    assert.equal(env.user.token, "new")
    assert.equal(env.messages.length, 0)
  }
})

test("注销失败保留会话；成功才清除持久 Token", async () => {
  let result: ReturnType<typeof ok> = failure(503)
  const env = environment(() => result)
  env.user.setToken("current")
  await assert.rejects(env.user.logout())
  assert.equal(env.user.token, "current")
  result = ok(null)
  await env.user.logout()
  assert.equal(env.user.token, "")
  assert.equal(env.storage.getItem("alphapulse.auth.token"), null)
})

test("匿名深链、登录回跳与已登录刷新使用真实身份", async () => {
  let count = 0
  const env = environment(() => {
    count++
    return ok({ username: "admin", is_admin: true })
  })
  env.guards.installGuards(env.router)
  await env.router.push("/calendar?date=2026-10-25")
  assert.equal(env.router.currentRoute.value.name, "login")
  assert.equal(env.router.currentRoute.value.query.redirect, "/calendar?date=2026-10-25")
  env.user.setToken("current")
  await env.router.push("/news")
  await env.router.push("/login?redirect=/calendar%3Fdate%3D2026-10-25")
  assert.equal(env.router.currentRoute.value.fullPath, "/calendar?date=2026-10-25")
  assert.equal(count, 1)
  await env.router.push("/news")
  assert.equal(count, 1)
  for (const target of ["//evil.test", "https://evil.test", "/\\evil.test", "/login?redirect=/login"])
    assert.equal(env.guards.safeRedirect(target), "/home")
})

test("身份恢复 401 保留深链回跳；403 不清身份，网络错误允许重试", async () => {
  for (const mode of ["expired", "forbidden", "network"]) {
    const env = environment(() => mode === "expired" ? failure(401) : mode === "network" ? failure(503) : ok({ username: "former-admin", is_admin: false }))
    env.user.setToken("current")
    env.guards.installGuards(env.router)
    await env.router.push("/calendar?date=today")
    const route = env.router.currentRoute.value
    assert.equal(route.name, mode === "expired" ? "login" : mode === "forbidden" ? "forbidden" : "connection-error")
    assert.equal(env.user.token, mode === "expired" ? "" : "current")
    if (mode === "expired") assert.equal(route.query.redirect, "/calendar?date=today")
    if (mode === "forbidden") assert.equal(env.user.identity.username, "former-admin")
  }
})

test("共享来源缓存只有一个在途请求，切换会话清空且丢弃旧响应", async () => {
  const pending = Promise.withResolvers<any>()
  let count = 0
  const env = environment(() => {
    count++
    return pending.promise
  })
  env.user.setToken("old")
  const sources = env.load("@/pinia/stores/sources").useSourcesStore()
  const first = sources.load()
  assert.equal(first, sources.load())
  const rejected = assert.rejects(first, (error: any) => axios.isCancel(error))
  await setImmediate()
  env.user.setToken("new")
  pending.resolve(ok({ sources: [{ code: "private", name: "old" }] }))
  await rejected
  assert.equal(count, 1)
  assert.deepEqual(Object.keys(sources.names), [])
})

test("确认框结束后不能以新会话发布旧页面的 Skill", async () => {
  const confirm = Promise.withResolvers<void>()
  const user = vue.reactive({ token: "old", sessionVersion: 1 })
  let published = 0
  const node = { id: 1, kind: "skill", name: "test" }
  const load = moduleLoader({
    "vue": { ...vue, onBeforeUnmount() {} },
    "@/pinia/stores/user": { useUserStore: () => user },
    "@@/apis/skills": {
      listSkillTreeApi: async () => [node],
      listSkillVersionsApi: async () => [{ id: 1, version_no: 1, content: "text" }],
      publishSkillVersionApi: async () => {
        published++
      }
    },
    "@@/components/SkillTree/index.vue": {},
    "@@/components/SkillVersionDrawer/index.vue": {},
    "@@/utils/markdown": { renderMarkdown: (text: string) => text }
  }, {
    ...vue,
    onMounted() {},
    onBeforeUnmount() {},
    onBeforeRouteLeave() {},
    ElMessage: { success() {} },
    ElMessageBox: { confirm: () => confirm.promise }
  })
  const page = load("@/pages/skills/index.vue").default.setup({}, { expose() {} })
  await page.loadTree()
  await page.select(node)
  const operation = page.publishLatest()
  await setImmediate()
  user.sessionVersion++
  confirm.resolve()
  await assert.rejects(operation, (error: any) => axios.isCancel(error))
  assert.equal(published, 0)
})

test("OpenAPI 仅显式原样返回；201/202 保留业务数据与 null", async () => {
  let result: { status: number, body: unknown } = { status: 200, body: { openapi: "3.1.0", info: { title: "test", version: "1" }, paths: {} } }
  const env = environment(() => result)
  env.user.setToken("current")
  const spec = await env.load("@@/apis/docs").getOpenApiApi()
  assert.equal(spec.openapi, "3.1.0")
  await assert.rejects(env.request({ url: "/news", method: "get" }), /响应格式无效/)
  for (const status of [201, 202]) {
    result = { status, body: { code: 0, data: null, message: "" } }
    assert.equal(await env.request({ url: "/write", method: "post" }), null)
  }
})
