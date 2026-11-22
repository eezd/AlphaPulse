import assert from "node:assert/strict"
import test from "node:test"
import { setImmediate } from "node:timers/promises"
import * as vue from "vue"
import { moduleLoader } from "./module-loader.ts"

function settingsHarness(api: Record<string, any> = {}) {
  let current = true
  let mounted = () => {}
  let confirms = 0
  const writes: any[] = []
  const events: any[] = []
  const confirmationTexts: string[] = []
  const defaults = () => ({
    enabled: false,
    control_revision: 2,
    config: {
      concurrency: 3,
      analysis: { model: "one" },
      period: { model: "two" },
      daily: { model: "three" }
    },
    telegram: { enabled: false, configured: true, token_hint: "***" }
  })
  const callbacks = {
    getSettingsApi: async () => defaults(),
    getCoverageApi: async () => ({ analyses: { pending: 5 } }),
    saveSettingsApi: async (body: any) => writes.push(body),
    enableApi: async (revision: number) => writes.push(["enable", revision]),
    pauseApi: async (revision: number) => writes.push(["pause", revision]),
    saveTelegramApi: async (body: any) => writes.push(body),
    getSkillTemplatesApi: async () => [{ stage: "analysis", content: "template" }],
    initializeSkillTemplatesApi: async (revision: number) => {
      writes.push(["initialize", revision])
      return ["analysis", "period", "daily"].map((stage, index) => ({ stage, skill_id: index + 10 }))
    },
    ...api
  }
  const load = moduleLoader(
    {
      "@@/apis/briefings": callbacks,
      "@@/apis/settings": {
        listProviderModelsApi: api.listProviderModelsApi ?? (async () => ["provider-default", "one", "two", "three"]),
        listProvidersApi: async () => [
          { id: 1, model: "provider-default", reasoning_effort: "high" }
        ]
      },
      "@@/apis/skills": {
        listSkillTreeApi: async () => [
          {
            id: 1,
            scope: "news",
            kind: "skill",
            enabled: true,
            published_version_no: 1
          },
          {
            id: 2,
            scope: "chat",
            kind: "skill",
            enabled: true,
            published_version_no: 1
          },
          {
            id: 3,
            scope: "news",
            kind: "skill",
            enabled: true,
            published_version_no: null
          }
        ]
      },
      "@@/composables/useSessionScope": {
        useSessionScope: () => ({ isCurrent: () => current })
      }
    },
    {
      ...vue,
      onMounted(fn: () => void) {
        mounted = fn
      },
      ElMessageBox: {
        confirm: (text: string) => {
          confirms++
          confirmationTexts.push(text)
          return api.confirm?.() ?? Promise.resolve()
        }
      },
      navigator: { clipboard: { writeText: async () => {} } },
      ElMessage: { warning() {}, success() {} }
    }
  )
  const page = load(
    "@/pages/home/components/BriefingSettings.vue"
  ).default.setup(
    {},
    { expose() {}, emit: (...args: any[]) => events.push(args) }
  )
  return {
    page,
    writes,
    events,
    confirmationTexts,
    defaults,
    get confirms() {
      return confirms
    },
    invalidate() {
      current = false
    },
    mount() {
      mounted()
    }
  }
}

test("配置与核查仅本地读取，筛选已发布新闻 Skill，不启用模型或自动发布", async () => {
  const h = settingsHarness()
  await h.page.load()
  assert.equal(h.page.settings.value.enabled, false)
  assert.equal(h.page.coverage.value.analyses.pending, 5)
  assert.deepEqual(
    Array.from(h.page.skills.value, (skill: any) => skill.id),
    [1]
  )
  assert.equal(h.page.form.daily.model, "three")
  assert.equal(h.page.tg.token, "")
  assert.equal(h.writes.length, 0)
})

test("配置读取拒绝旧响应、旧会话和卸载后的响应，失败可重新读取", async () => {
  const pending: ReturnType<typeof Promise.withResolvers<any>>[] = []
  const h = settingsHarness({
    getSettingsApi: () => {
      const request = Promise.withResolvers<any>()
      pending.push(request)
      return request.promise
    }
  })
  const old = h.page.load()
  const latest = h.page.load()
  pending[1].resolve({ ...h.defaults(), control_revision: 3 })
  await latest
  pending[0].resolve(h.defaults())
  await old
  assert.equal(h.page.settings.value.control_revision, 3)
  const failing = h.page.load()
  pending[2].reject(new Error("load failed"))
  await failing
  assert.equal(h.page.failed.value, true)
  const stale = h.page.load()
  h.invalidate()
  pending[3].resolve({ ...h.defaults(), control_revision: 4 })
  await stale
  await h.page.load()
  assert.equal(pending.length, 4)
  assert.equal(h.page.settings.value.control_revision, 3)
})

test("三组模型和思考强度分别保存；请求快照不受后续草稿改变影响", async () => {
  const saving = Promise.withResolvers<void>()
  let body: any
  const h = settingsHarness({
    saveSettingsApi: (value: any) => {
      body = value
      return saving.promise
    }
  })
  await h.page.load()
  h.page.form.analysis.reasoning_effort = "low"
  h.page.form.period.reasoning_effort = "medium"
  h.page.form.daily.reasoning_effort = "high"
  const request = h.page.save()
  await h.page.save()
  h.page.form.analysis.model = "next-draft"
  assert.equal(body.analysis.model, "one")
  assert.equal(body.base_revision, 2)
  assert.deepEqual(
    [
      body.analysis.reasoning_effort,
      body.period.reasoning_effort,
      body.daily.reasoning_effort
    ],
    ["low", "medium", "high"]
  )
  assert.equal("index" in body, false)
  assert.equal("research" in body, false)
  saving.reject(new Error("save failed"))
  await assert.rejects(request, /save failed/)
  assert.equal(h.page.form.analysis.model, "next-draft")
  assert.equal(h.page.busy.value, false)
  h.invalidate()
  await h.page.save()
  assert.equal(h.events.length, 0)
})

test("更换供应商使用其默认模型，但不更改共享供应商思考强度", async () => {
  const h = settingsHarness()
  await h.page.load()
  h.page.form.period.provider_id = 1
  await h.page.providerChanged("period")
  assert.equal(h.page.form.period.model, "provider-default")
  assert.equal(h.page.form.period.reasoning_effort, "medium")
  assert.equal(h.page.providers.value[0].reasoning_effort, "high")
})

test("模型目录替换旧响应，切换接入不沿用旧模型，目录失败可重试", async () => {
  const pending: ReturnType<typeof Promise.withResolvers<string[]>>[] = []
  const h = settingsHarness({ listProviderModelsApi: () => {
    const request = Promise.withResolvers<string[]>()
    pending.push(request)
    return request.promise
  } })
  await h.page.load()
  const old = h.page.loadModels(1)
  const latest = h.page.loadModels(1, true)
  pending[1].resolve(["new-model"])
  await latest
  pending[0].resolve(["old-model"])
  await old
  assert.deepEqual(Array.from(h.page.catalogs[1].items), ["new-model"])
  h.page.form.period.provider_id = 1
  await h.page.providerChanged("period")
  assert.equal(h.page.form.period.model, "")
  const failing = h.page.loadModels(1, true)
  pending[2].reject(new Error("catalog unavailable"))
  await failing
  assert.match(h.page.catalogs[1].error, /读取失败/)
  const retry = h.page.loadModels(1, true)
  pending[3].resolve(["provider-default"])
  await retry
  assert.equal(h.page.catalogs[1].error, "")
  const stale = h.page.loadModels(1, true)
  h.invalidate()
  pending[4].resolve(["stale-session"])
  await stale
  assert.deepEqual(Array.from(h.page.catalogs[1].items), ["provider-default"])
})

test("模型列表晚到不会改写新接入或用户已选模型", async () => {
  const pending = Promise.withResolvers<string[]>()
  const h = settingsHarness({ listProviderModelsApi: () => pending.promise })
  await h.page.load()
  h.page.form.analysis.provider_id = 1
  const request = h.page.providerChanged("analysis")
  h.page.form.analysis.provider_id = 2
  h.page.form.analysis.model = "chosen-for-two"
  pending.resolve(["provider-default"])
  await request
  assert.equal(h.page.form.analysis.model, "chosen-for-two")
})

test("初始化只提交一次，填入空 Skill，不覆盖已有选择、模型和回补日期", async () => {
  const confirmation = Promise.withResolvers<void>()
  const h = settingsHarness({ confirm: () => confirmation.promise })
  await h.page.load()
  h.page.form.analysis.skill_id = 99
  h.page.form.backfill_start = "2026-10-01"
  const request = h.page.initializeTemplates()
  await h.page.initializeTemplates()
  assert.equal(h.confirms, 1)
  confirmation.resolve()
  await request
  assert.deepEqual(h.writes.slice(), [["initialize", 2]])
  assert.equal(h.page.form.analysis.skill_id, 99)
  assert.equal(h.page.form.period.skill_id, 11)
  assert.equal(h.page.form.daily.skill_id, 12)
  assert.equal(h.page.form.daily.model, "three")
  assert.equal(h.page.form.backfill_start, "2026-10-01")
  await h.page.save()
  assert.equal(h.writes[1].backfill_start, "2026-10-01")
  assert.equal("period_backfill_start" in h.writes[1], false)
  assert.equal("daily_backfill_start" in h.writes[1], false)
})

for (const reason of ["session", "revision", "cancel"] as const) {
  test(`初始化确认期间 ${reason} 变化不发布 Skill`, async () => {
    const confirmation = Promise.withResolvers<void>()
    const h = settingsHarness({ confirm: () => confirmation.promise })
    await h.page.load()
    const request = h.page.initializeTemplates()
    if (reason === "session") h.invalidate()
    if (reason === "revision") h.page.settings.value.control_revision++
    if (reason === "cancel") confirmation.reject(new Error("cancel"))
    else confirmation.resolve()
    await request
    assert.equal(h.writes.length, 0)
  })
}

test("初始化响应晚于配置刷新时不覆盖新表单", async () => {
  const pending = Promise.withResolvers<any>()
  const h = settingsHarness({ initializeSkillTemplatesApi: () => pending.promise })
  await h.page.load()
  const request = h.page.initializeTemplates()
  await setImmediate()
  await h.page.load()
  pending.resolve([{ stage: "daily", skill_id: 100 }])
  await request
  assert.equal(h.page.form.daily.skill_id, null)
  assert.equal(h.page.busy.value, false)
})

for (const enabled of [false, true]) {
  test(`${enabled ? "暂停" : "启用"}只确认一次并提交已读 revision，不提交未保存草稿`, async () => {
    const confirmation = Promise.withResolvers<void>()
    const h = settingsHarness({ confirm: () => confirmation.promise })
    await h.page.load()
    h.page.settings.value.enabled = enabled
    h.page.form.daily.model = "unsaved"
    h.page.form.backfill_start = "2026-10-01"
    h.page.settings.value.config.backfill_start = "2026-11-01"
    const request = h.page.control()
    await h.page.control()
    assert.equal(h.confirms, 1)
    if (!enabled) {
      assert.match(h.confirmationTexts[0], /2026-11-01.*00:00.*单篇分析、分时报告和日报/)
      assert.doesNotMatch(h.confirmationTexts[0], /2026-10-01/)
    }
    confirmation.resolve()
    await request
    assert.deepEqual(h.writes, [[enabled ? "pause" : "enable", 2]])
  })
}

for (const reason of ["session", "revision", "cancel"] as const) {
  test(`确认期间 ${reason} 变化不会执行过时开关操作`, async () => {
    const confirmation = Promise.withResolvers<void>()
    const h = settingsHarness({ confirm: () => confirmation.promise })
    await h.page.load()
    const request = h.page.control()
    if (reason === "session") h.invalidate()
    if (reason === "revision") h.page.settings.value.control_revision++
    if (reason === "cancel") confirmation.reject(new Error("cancel"))
    else confirmation.resolve()
    await request
    assert.equal(h.writes.length, 0)
    if (reason !== "session") assert.equal(h.page.busy.value, false)
  })
}

test("TG 空口令保留原密钥；保存后清空输入；旧会话不能提交", async () => {
  const h = settingsHarness()
  await h.page.load()
  h.page.tg.enabled = true
  await h.page.saveTelegram()
  assert.equal("token" in h.writes[0], false)
  h.page.tg.token = "synthetic-token"
  h.page.tg.chat_id = "-100123456"
  await h.page.saveTelegram()
  assert.equal(h.writes[1].token, "synthetic-token")
  assert.equal(h.page.tg.token, "")
  h.invalidate()
  await h.page.saveTelegram()
  assert.equal(h.writes.length, 2)
})
