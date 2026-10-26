import assert from "node:assert/strict"
import test from "node:test"
import { moduleLoader, require } from "./module-loader.ts"

const vue = require("vue")

test("验证码刷新保留最新响应；登录锁覆盖导航，失败刷新挑战", async () => {
  const first = Promise.withResolvers<any>()
  const second = Promise.withResolvers<any>()
  const signed = Promise.withResolvers<any>()
  const navigated = Promise.withResolvers<any>()
  let challenges = 0
  let logins = 0
  let fail = false
  const user = { token: "", setToken(value: string) {
    this.token = value
  } }
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/router/guards": { safeRedirect: () => "/calendar" },
    "@@/apis/auth": {
      getCaptchaApi: () => ++challenges === 1 ? first.promise : challenges === 2 ? second.promise : Promise.resolve({ enabled: true, captcha_id: "retry", image: "png", expires_in: 120 }),
      loginApi: () => {
        logins++
        return fail ? Promise.reject(new Error("验证码失败")) : signed.promise
      }
    }
  }, {
    ...vue,
    useRoute: () => ({ query: { redirect: "/calendar" } }),
    useRouter: () => ({ replace: () => navigated.promise }),
    onMounted() {},
    onBeforeUnmount() {},
    ElMessage: { warning() {} }
  })
  const page = load("@/pages/login/index.vue").default.setup({}, { expose() {} })
  page.formRef.value = { validate: async () => true }
  const a = page.refreshCaptcha()
  const b = page.refreshCaptcha()
  second.resolve({ enabled: true, captcha_id: "new", image: "new-png", expires_in: 120 })
  await b
  first.resolve({ enabled: true, captcha_id: "old", image: "old-png", expires_in: 120 })
  await a
  assert.equal(page.form.captcha_id, "new")
  assert.equal(page.image.value, "new-png")
  Object.assign(page.form, { username: "admin", password: "password", code: "ABCDE" })
  const sending = page.submit()
  await page.submit()
  await Promise.resolve()
  assert.equal(logins, 1)
  signed.resolve({ token: "new-token" })
  await Promise.resolve()
  assert.equal(page.loading.value, true)
  navigated.resolve(undefined)
  await sending
  assert.equal(user.token, "new-token")
  assert.equal(page.loading.value, false)
  fail = true
  await assert.rejects(page.submit())
  assert.equal(page.form.captcha_id, "retry")
  assert.equal(page.form.code, "")
})

test("验证码关闭时只提交账号密码，失败无需刷新挑战", async () => {
  let challenges = 0
  let fail = false
  const bodies: unknown[] = []
  const user = { token: "", setToken(value: string) {
    this.token = value
  } }
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => user },
    "@/router/guards": { safeRedirect: () => "/news" },
    "@@/apis/auth": {
      getCaptchaApi: async () => {
        challenges++
        return { enabled: false }
      },
      loginApi: async (body: unknown) => {
        bodies.push(body)
        if (fail) throw new Error("用户名或密码错误")
        return { token: "password-login-token" }
      }
    }
  }, {
    ...vue,
    useRoute: () => ({ query: {} }),
    useRouter: () => ({ replace: async () => {} }),
    onMounted() {},
    onBeforeUnmount() {},
    ElMessage: { warning() {
      assert.fail("不应要求验证码")
    } }
  })
  const page = load("@/pages/login/index.vue").default.setup({}, { expose() {} })
  page.formRef.value = { validate: async () => true }
  await page.refreshCaptcha()
  assert.equal(page.captchaEnabled.value, false)
  Object.assign(page.form, { username: "admin", password: "password" })
  await page.submit()
  assert.deepEqual(JSON.parse(JSON.stringify(bodies)), [{ username: "admin", password: "password" }])
  assert.equal(user.token, "password-login-token")
  fail = true
  await assert.rejects(page.submit())
  assert.equal(challenges, 1)
  assert.equal(page.loading.value, false)
})

test("验证配置读取失败阻止登录，重试成功后解除；表单失败不发送请求", async () => {
  let fail = true
  let logins = 0
  const load = moduleLoader({
    "@/pinia/stores/user": { useUserStore: () => ({ sessionVersion: 0 }) },
    "@/router/guards": { safeRedirect: () => "/news" },
    "@@/apis/auth": {
      getCaptchaApi: async () => {
        if (fail) throw new Error("配置读取失败")
        return { enabled: false }
      },
      loginApi: () => {
        logins++
      }
    }
  }, { ...vue, useRoute: () => ({ query: {} }), useRouter: () => ({}), onMounted() {}, onBeforeUnmount() {} })
  const page = load("@/pages/login/index.vue").default.setup({}, { expose() {} })
  await assert.rejects(page.refreshCaptcha(), /配置读取失败/)
  await page.submit()
  assert.equal(logins, 0)
  assert.equal(page.captchaReady.value, false)
  assert.equal(page.captchaError.value, "配置读取失败")
  fail = false
  await page.refreshCaptcha()
  page.formRef.value = { validate: async () => {
    throw new Error("表单无效")
  } }
  await assert.rejects(page.submit(), /表单无效/)
  assert.equal(logins, 0)
  assert.equal(page.captchaReady.value, true)
  assert.equal(page.loading.value, false)
})
