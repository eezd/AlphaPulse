<script setup lang="ts">
import type { SkillNode } from "@@/apis/skills/type"
import type * as Risk from "@/common/apis/risk/type"
import type { Provider } from "@/common/apis/settings/type"
import { listSkillTreeApi } from "@@/apis/skills"
import { EFFORTS } from "@@/constants/ai"
import { formatTime } from "@@/utils/text"
import * as api from "@/common/apis/risk"
import { listProviderModelsApi, listProvidersApi } from "@/common/apis/settings"
import { useUserStore } from "@/pinia/stores/user"

import EventResearch from "./components/EventResearch.vue"

const eventResearch = ref<InstanceType<typeof EventResearch> | null>(null)
function openReading(row: Risk.Reading) {
  if (row.kind === "event") void eventResearch.value?.open(row.id)
  else void openEvent(row.id)
}
const user = useUserStore()
const route = useRoute()
const hours = ref(72)
const stars = ref(0)
const importance = ref(0)
const category = ref("")
const asset = ref("")
const taskStage = ref("grouping")
const newsSkills = ref<SkillNode[]>([])
const secondModels = ref<string[]>([])
const secondModelsError = ref("")
const secondModelsLoading = ref(false)
let secondModelsSequence = 0
const archive = ref(false)
const tag = ref("")
const readingPage = ref(1)
const thirdModels = ref<string[]>([])
const thirdError = ref("")
let thirdSequence = 0
const readingRows = computed(() => overview.value?.briefing?.reading.slice((readingPage.value - 1) * 20, readingPage.value * 20) ?? [])
function readingTitle(id: number, kind = "package") {
  return overview.value?.briefing?.inputs.find(p => p.id === id && (p.kind ?? "package") === kind)?.result.title ?? `包 #${id}`
}
const q = ref("")
const range = ref<[Date, Date] | null>(null)
const page = ref(1)
const total = ref(0)
const overview = ref<Risk.Overview | null>(null)
const events = ref<Risk.RiskEvent[]>([])
const settings = ref<Risk.Settings | null>(null)
const providers = ref<Provider[]>([])
const showSettings = ref(false)
const models = ref<string[]>([])
const modelsLoading = ref(false)
const modelsLoaded = ref(false)
const modelsError = ref("")
const detail = ref<Awaited<ReturnType<typeof api.getRiskEventApi>> | null>(null)
const showDetail = ref(false)
const failures = ref<Risk.Analysis[]>([])
const showFailures = ref(false)
const loading = ref(false)
const failed = ref(false)
const saving = ref(false)
const concurrencySaving = ref(false)
let sequence = 0
let detailSequence = 0
let modelsSequence = 0
let timer: ReturnType<typeof setInterval> | undefined
const labels: Record<string, string> = { bullish: "偏多", bearish: "偏空", mixed: "震荡／分歧", neutral: "中性", unknown: "无法判断", official: "官方公告", reported: "媒体报道", unverified: "待核实", refuted: "更正／辟谣", insufficient: "信息不足", low: "低", medium: "中", high: "高" }
const starText = (value: number) => value ? "★".repeat(value) : "无明显风险"
const levels = ["未评级", "背景信息", "局部进展", "重要变化", "广泛影响", "重大变化"]
async function load() {
  const seq = ++sequence
  const version = user.sessionVersion
  loading.value = true
  try {
    const [summary, list] = await Promise.all([api.getRiskOverviewApi(hours.value), api.listRiskEventsApi({ importance: importance.value, tag: tag.value, hours: archive.value ? undefined : hours.value, stars: stars.value, q: q.value, page: page.value, category: category.value, asset: asset.value, start: archive.value ? range.value?.[0].toISOString() : undefined, end: archive.value ? range.value?.[1].toISOString() : undefined })])
    if (seq !== sequence || version !== user.sessionVersion) return
    overview.value = summary
    readingPage.value = Math.min(readingPage.value, Math.max(1, Math.ceil((summary.briefing?.reading.length ?? 0) / 20)))
    events.value = list.items
    total.value = list.total
    failed.value = false
  } catch {
    if (seq === sequence && version === user.sessionVersion) failed.value = true
  } finally {
    if (seq === sequence) loading.value = false
  }
}
async function openSettings() {
  const version = user.sessionVersion
  const [config, choices, skills] = await Promise.all([api.getRiskSettingsApi(), listProvidersApi(), listSkillTreeApi()])
  if (version !== user.sessionVersion) return
  newsSkills.value = skills.filter(s => s.scope === "news" && s.kind === "skill")
  settings.value = config
  providers.value = choices
  showSettings.value = true
}
async function loadModels() {
  const seq = ++modelsSequence
  const version = user.sessionVersion
  const id = settings.value?.provider_id
  models.value = []
  modelsError.value = ""
  modelsLoaded.value = false
  modelsLoading.value = false
  if (!showSettings.value || !id) return
  modelsLoading.value = true
  try {
    const choices = await listProviderModelsApi(id)
    if (seq !== modelsSequence || version !== user.sessionVersion) return
    models.value = choices
    modelsLoaded.value = true
  } catch (error) {
    if (seq === modelsSequence && version === user.sessionVersion)
      modelsError.value = error instanceof Error ? error.message : "读取模型列表失败"
  } finally {
    if (seq === modelsSequence && version === user.sessionVersion) modelsLoading.value = false
  }
}
async function loadSecondModels() {
  const seq = ++secondModelsSequence
  const version = user.sessionVersion
  const id = settings.value?.second_provider_id
  secondModels.value = []
  secondModelsError.value = ""
  secondModelsLoading.value = false
  if (!showSettings.value || !id) return
  secondModelsLoading.value = true
  try {
    const choices = await listProviderModelsApi(id)
    if (seq === secondModelsSequence && version === user.sessionVersion) secondModels.value = choices
  } catch (error) {
    if (seq === secondModelsSequence && version === user.sessionVersion) secondModelsError.value = error instanceof Error ? error.message : "读取模型列表失败"
  } finally {
    if (seq === secondModelsSequence && version === user.sessionVersion) secondModelsLoading.value = false
  }
}
async function loadThirdModels() {
  const seq = ++thirdSequence
  const version = user.sessionVersion
  const id = settings.value?.third_provider_id
  thirdModels.value = []
  thirdError.value = ""
  if (!showSettings.value || !id) return
  try {
    const rows = await listProviderModelsApi(id)
    if (seq === thirdSequence && version === user.sessionVersion) thirdModels.value = rows
  } catch (error) {
    if (seq === thirdSequence && version === user.sessionVersion) thirdError.value = error instanceof Error ? error.message : "模型列表读取失败"
  }
}
async function createTemplates() {
  const version = user.sessionVersion
  const value = await api.createNewsSkillTemplatesApi(true)
  if (version !== user.sessionVersion || !settings.value) return
  Object.assign(settings.value, value)
  const skills = await listSkillTreeApi()
  if (version !== user.sessionVersion) return
  newsSkills.value = skills.filter(s => s.scope === "news" && s.kind === "skill")
  ElMessage.success("默认新闻 Skills 已准备好，请前往 Skills 页面审阅并发布")
}
async function saveConcurrency() {
  if (!settings.value || concurrencySaving.value) return
  const version = user.sessionVersion
  concurrencySaving.value = true
  try {
    await api.saveRiskConcurrencyApi(settings.value.max_concurrency)
    if (version !== user.sessionVersion) return
    ElMessage.success("并发上限已保存；降低上限时等待在途任务完成")
    await load()
  } finally {
    if (version === user.sessionVersion) concurrencySaving.value = false
  }
}
async function save(enable: boolean) {
  if (!settings.value) return
  const version = user.sessionVersion
  saving.value = true
  try {
    const data = { ...settings.value, model: settings.value.model.trim() }
    if (enable) await api.enableRiskApi(data)
    else await api.saveRiskSettingsApi({ ...data, enabled: false })
    if (version !== user.sessionVersion) return
    showSettings.value = false
    await load()
  } finally {
    saving.value = false
  }
}
async function openEvent(id: number) {
  const seq = ++detailSequence
  const version = user.sessionVersion
  detail.value = null
  showDetail.value = true
  const data = await api.getRiskEventApi(id)
  if (seq === detailSequence && version === user.sessionVersion) detail.value = data
}
async function openFailures() {
  const version = user.sessionVersion
  const stage = taskStage.value
  const data = await api.listRiskAnalysesApi({ failed: true, stage })
  if (stage !== taskStage.value) return
  if (version !== user.sessionVersion) return
  failures.value = data
  showFailures.value = true
}
async function retry(id: number) {
  const version = user.sessionVersion
  await api.retryRiskAnalysisApi(id, taskStage.value)
  if (version === user.sessionVersion) await openFailures()
}
function search() {
  if (page.value === 1) void load()
  else page.value = 1
}
watch([hours, stars, importance, archive, range, category, asset, tag], search)
watch(page, () => void load())
watch([showSettings, () => settings.value?.provider_id], () => void loadModels())
watch([showSettings, () => settings.value?.second_provider_id], () => void loadSecondModels())
watch([showSettings, () => settings.value?.third_provider_id], () => void loadThirdModels())
watch(taskStage, () => {
  if (showFailures.value) void openFailures()
})
watch(() => user.sessionVersion, () => {
  concurrencySaving.value = false
  thirdSequence++
  secondModelsSequence++
  secondModels.value = []
  modelsSequence++
  showSettings.value = false
  settings.value = null
  models.value = []
  modelsLoading.value = false
})
watch(showDetail, (visible) => {
  if (!visible) {
    detailSequence++
    detail.value = null
  }
})
onMounted(() => {
  void load()
  const summaryId = Number(route.query.summary)
  if (Number.isSafeInteger(summaryId) && summaryId > 0) void openEvent(summaryId)
  timer = setInterval(() => {
    if (!document.hidden && !loading.value) void load()
  }, 30000)
})
onBeforeUnmount(() => {
  sequence++
  detailSequence++
  modelsSequence++
  thirdSequence++
  secondModelsSequence++
  clearInterval(timer)
})
</script>

<template>
  <div class="mx-auto max-w-7xl flex flex-col gap-4">
    <div class="flex flex-wrap items-center gap-3">
      <div class="flex-1">
        <h1 class="m-0 text-xl font-semibold">
          72小时新闻阅读与研判
        </h1><p class="mb-0 mt-1 text-sm text-slate-500">
          跨领域信息、事实核验、市场与社会趋势
        </p>
      </div>
      <el-button @click="openSettings">
        分析设置
      </el-button><el-button @click="load">
        刷新
      </el-button>
    </div>
    <el-alert v-if="failed" type="error" :closable="false" title="读取失败，保留已有结果，请重试" />
    <div class="flex flex-wrap items-center gap-3">
      <el-radio-group v-model="hours">
        <el-radio-button :value="24">
          24 小时
        </el-radio-button><el-radio-button :value="48">
          48 小时
        </el-radio-button><el-radio-button :value="72">
          72 小时
        </el-radio-button><el-radio-button :value="168">
          7 天
        </el-radio-button>
      </el-radio-group>
      <span class="text-xs text-slate-500">仅处理新流程首次启用后发布或源站修订的新闻</span>
    </div>
    <el-card v-if="overview" shadow="never">
      <div class="flex flex-wrap gap-5">
        <div v-for="level in [5, 4, 3, 2, 1]" :key="level">
          <div class="text-amber-600">
            <span :title="levels[level]">{{ starText(level) }}</span>
          </div><strong class="text-2xl">{{ overview.counts[String(level)] ?? 0 }}</strong><span class="ml-1 text-sm text-slate-500">包</span>
        </div>
      </div>
      <p class="mb-0 text-sm text-slate-500">
        初筛完成 {{ overview.coverage.done }} / {{ overview.coverage.total }} 条 · 待处理 {{ overview.coverage.pending }} · 处理中 {{ overview.coverage.processing }} · <el-button link type="danger" @click="openFailures">
          失败 {{ overview.coverage.failed }}
        </el-button>
      </p>
      <p v-if="overview.concurrency">
        AI 并发：{{ overview.concurrency.running ?? "未知" }} / {{ overview.concurrency.limit }} · 排队 {{ overview.concurrency.queued }}
        <span v-for="(count, stage) in overview.concurrency.stages" :key="stage" class="ml-2">{{ ({ grouping: "分包", analysis: "研判", events: "事件", outlook: "阅读", history: "历史" } as Record<string, string>)[stage] }} {{ count }}</span>
      </p>
      <p>包研判：完成 {{ overview.stage2.done ?? 0 }} · 待更新 {{ overview.stage2.pending ?? 0 }} · 处理中 {{ overview.stage2.processing ?? 0 }} · 失败 {{ overview.stage2.failed ?? 0 }}</p>
      <p v-if="overview.stage3">
        阅读编排：{{ overview.stage3.state === 'processing' ? '处理中' : overview.stage3.state === 'failed' ? '失败' : overview.stage3.pending ? '等待聚合更新' : '已更新' }}
      </p>
      <el-alert v-if="overview.coverage.done < overview.coverage.total" class="mt-3" type="info" :closable="false" title="分析覆盖尚未完整；没有风险事件不代表没有风险。" />
      <p class="mb-0 text-xs text-slate-500">
        Worker 心跳：{{ formatTime(overview.heartbeat) }} · 数据读取：{{ formatTime(overview.as_of) }}
      </p>
    </el-card>
    <el-card shadow="never">
      <template #header>
        近72小时完整阅读清单 · 按优先级排序
      </template>
      <template v-if="overview?.briefing">
        <p>生成于 {{ formatTime(overview.briefing.as_of) }} · 共 {{ overview.briefing.reading.length }} 项事件与独立包</p>
        <el-alert v-if="overview.stale" title="阅读稿可能已过期或更新失败，当前保留上次成功结果" type="warning" :closable="false" />
        <div v-for="(row, index) in readingRows" :key="`${row.kind}:${row.id}`" class="my-3">
          <el-button link @click="openReading(row)">
            {{ (readingPage - 1) * 20 + index + 1 }}. {{ readingTitle(row.id, row.kind) }}
          </el-button>
          <p>{{ row.reason }} · 优先级 {{ row.priority }} · 依据版本 {{ row.revision }}</p>
        </div>
        <el-pagination v-model:current-page="readingPage" :total="overview.briefing.reading.length" :page-size="20" layout="prev, pager, next, total" />
        <p><b>拓展阅读</b></p>
        <div v-for="row in overview.briefing.expansion" :key="`${row.kind}:${row.id}`">
          <el-button link @click="openReading(row)">
            {{ readingTitle(row.id, row.kind) }}
          </el-button><p>{{ row.reason }}</p>
        </div>
        <template v-if="overview.briefing.synthesis">
          <p><b>综合判断：</b>{{ overview.briefing.synthesis.summary }}</p>
          <p><b>市场影响：</b>{{ overview.briefing.synthesis.market }}</p>
          <div v-for="(trend, index) in [overview.briefing.synthesis.short_term, overview.briefing.synthesis.medium_term]" :key="index">
            <p><b>{{ index === 0 ? '未来24～72小时' : '未来1～4周' }}：</b>{{ trend.summary }}</p>
            <p>成立条件：{{ trend.conditions }}</p><p>反向证据：{{ trend.counter_evidence }}</p><p>关注点：{{ trend.watchpoints }}</p>
          </div>
        </template>
      </template>
      <el-empty v-else description="暂无阅读稿，请配置三个阶段模型并启用" />
      <el-alert v-if="overview?.error" :title="overview.error" type="warning" :closable="false" />
    </el-card>
    <EventResearch ref="eventResearch" @package="openEvent" />
    <div class="flex flex-wrap gap-3">
      <el-switch v-model="archive" active-text="历史归档" />
      <el-select v-model="importance" class="w-40">
        <el-option :value="0" label="全部重要性" /><el-option v-for="n in 5" :key="n" :value="n" :label="`重要性${n}星及以上`" />
      </el-select>
      <el-select v-model="stars" class="w-40">
        <el-option :value="0" label="全部风险评级" /><el-option v-for="n in 5" :key="n" :value="n" :label="`${n} 星及以上`" />
      </el-select>
      <el-input v-model="category" class="w-40" clearable placeholder="内容分类" />
      <el-input v-model="tag" class="w-40" clearable placeholder="TAG标签" />
      <el-input v-model="asset" class="w-40" clearable placeholder="受影响资产" />
      <el-input v-model="q" class="w-60" clearable placeholder="事件关键词" @keyup.enter="search" @clear="search" />
      <el-date-picker v-if="archive" v-model="range" type="datetimerange" start-placeholder="开始" end-placeholder="结束" />
    </div>
    <div v-loading="loading" class="flex flex-col gap-3">
      <el-card v-for="event in events" :key="event.id" shadow="never" class="cursor-pointer" @click="openEvent(event.id)">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-amber-600">{{ event.result?.verification === 'insufficient' ? '信息不足，无法判断' : starText(event.risk_stars) }}</span><el-tag size="small">
            {{ labels[event.result?.verification ?? "unknown"] }}
          </el-tag><el-tag size="small" type="info">
            {{ labels[event.result?.direction ?? "unknown"] }}
          </el-tag><span class="ml-auto text-xs text-slate-500">最新进展 {{ formatTime(event.updated_at) }}</span>
        </div>
        <span v-if="event.state !== 'done'" class="text-xs text-amber-600">{{ event.error ? "更新失败，保留上次总结" : "总结更新中" }}</span>
        <h3 class="my-2 text-base">
          {{ event.title }}
        </h3><p class="my-2 text-sm text-slate-600">
          {{ event.result?.summary }}
        </p><p class="mb-0 text-xs text-slate-500">
          重要性 {{ event.importance_stars }} 星 · {{ event.tags.join(" · ") }} · {{ event.entities.join(' · ') }} · {{ event.article_count }} 篇相关文章 · 历史最高 {{ starText(event.peak_stars) }} · 总结 {{ formatTime(event.analyzed_at) }}
        </p>
      </el-card>
      <el-empty v-if="!events.length && !loading" description="该范围内暂无已归档的风险事件" />
      <el-pagination v-model:current-page="page" :total="total" :page-size="20" layout="prev, pager, next, total" />
    </div>
    <el-dialog v-model="showSettings" title="新闻 AI 分析设置" width="min(600px, 95vw)">
      <el-form v-if="settings" label-position="top">
        <el-form-item label="第一阶段 Luna 接入配置">
          <el-select v-model="settings.provider_id" class="w-full">
            <el-option v-for="provider in providers" :key="provider.id" :value="provider.id" :label="provider.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="第一阶段模型名称">
          <div class="w-full flex gap-2">
            <el-select v-model="settings.model" class="min-w-0 flex-1" filterable allow-create default-first-option :loading="modelsLoading" :disabled="!settings.provider_id" placeholder="选择模型，或输入模型 ID 后回车">
              <el-option v-for="model in models" :key="model" :value="model" :label="model" />
            </el-select>
            <el-button :loading="modelsLoading" :disabled="!settings.provider_id" @click="loadModels">
              刷新列表
            </el-button>
          </div>
          <p v-if="modelsError" class="mb-0 w-full text-sm text-amber-600">
            {{ modelsError }}
          </p>
          <p v-else-if="modelsLoaded && !models.length" class="mb-0 text-sm text-slate-500">
            接口未返回可选模型，可手动填写模型 ID。
          </p>
        </el-form-item>
        <el-form-item label="第一阶段思考强度">
          <el-select v-model="settings.reasoning_effort" class="w-full" clearable :value-on-clear="null" placeholder="跟随接入配置">
            <el-option v-for="effort in EFFORTS" :key="effort" :value="effort" :label="effort" />
          </el-select>
        </el-form-item>
        <el-form-item label="第二阶段接入配置">
          <el-select v-model="settings.second_provider_id" class="w-full">
            <el-option v-for="provider in providers" :key="provider.id" :value="provider.id" :label="provider.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="第二阶段模型名称">
          <el-select v-model="settings.second_model" class="w-full" filterable allow-create default-first-option :loading="secondModelsLoading" :disabled="!settings.second_provider_id">
            <el-option v-for="model in secondModels" :key="model" :value="model" :label="model" />
          </el-select>
          <el-button :disabled="!settings.second_provider_id" @click="loadSecondModels">
            刷新列表
          </el-button>
          <p v-if="secondModelsError">
            {{ secondModelsError }}
          </p>
        </el-form-item>
        <el-form-item label="第二阶段思考强度">
          <el-select v-model="settings.second_reasoning_effort" class="w-full" clearable :value-on-clear="null" placeholder="跟随接入配置">
            <el-option v-for="effort in EFFORTS" :key="effort" :value="effort" :label="effort" />
          </el-select>
        </el-form-item>
        <el-form-item label="第三阶段高级模型接入配置">
          <el-select v-model="settings.third_provider_id" class="w-full">
            <el-option v-for="provider in providers" :key="provider.id" :value="provider.id" :label="provider.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="第三阶段模型名称">
          <el-select v-model="settings.third_model" class="w-full" filterable allow-create default-first-option>
            <el-option v-for="model in thirdModels" :key="model" :value="model" :label="model" />
          </el-select><el-button @click="loadThirdModels">
            刷新列表
          </el-button><p>{{ thirdError }}</p>
        </el-form-item>
        <el-form-item label="第三阶段思考强度">
          <el-select v-model="settings.third_reasoning_effort" class="w-full" clearable :value-on-clear="null" placeholder="跟随接入配置">
            <el-option v-for="effort in EFFORTS" :key="effort" :value="effort" :label="effort" />
          </el-select>
        </el-form-item>
        <p class="text-sm text-slate-500">
          思考强度留空时跟随接入配置；接入配置也未设置时，由模型默认处理。不支持所选强度的模型会在测试启用时明确报错。
        </p>
        <el-form-item v-for="field in (['grouping_skill_id', 'analysis_skill_id', 'outlook_skill_id'] as const)" :key="field" :label="({ grouping_skill_id: '初筛归并 Skill', analysis_skill_id: '包核验 Skill', outlook_skill_id: '高级阅读编排 Skill' })[field]">
          <el-select v-model="settings[field]" class="w-full">
            <el-option v-for="skill in newsSkills" :key="skill.id" :value="skill.id" :label="`${skill.path} · ${skill.published_version_no ? `已发布 v${skill.published_version_no}` : '未发布'}`" />
          </el-select>
        </el-form-item>
        <el-button @click="createTemplates">
          生成/更新默认新闻 Skills 草稿
        </el-button>
        <router-link to="/skills">
          编辑与发布 Skills
        </router-link>
        <el-form-item label="最大并发任务数">
          <el-input-number v-model="settings.max_concurrency" :min="1" :precision="0" :step="1" :disabled="concurrencySaving" />
          <el-button class="ml-2" :loading="concurrencySaving" @click="saveConcurrency">
            保存并发上限
          </el-button>
        </el-form-item>
        <p>所有新闻阶段共享名额，完成即补位；实时优先，历史使用空闲名额。测试并启用前请先暂停并等待在途任务完成。</p>
        <el-alert type="info" :closable="false" title="测试并启用会分别调用分包、中级研判和高级阅读编排模型。首次启用后仅处理新发布或源站修订的新闻；暂停后恢复沿用原起点。" />
        <p>当前状态：{{ settings.enabled ? '已启用' : '已关闭' }}</p>
        <el-button :loading="saving" @click="save(false)">
          保存并暂停分析
        </el-button><el-button type="primary" :loading="saving" @click="save(true)">
          测试并启用
        </el-button>
      </el-form>
    </el-dialog>
    <el-drawer v-model="showDetail" title="新闻包证据与评级历史" size="min(760px, 100vw)">
      <template v-if="detail">
        <el-button v-if="detail.event.main_event_id" link @click="eventResearch?.open(detail.event.main_event_id)">
          查看主事件时间线
        </el-button>
        <el-button v-for="id in detail.event.replacement_ids" :key="id" link @click="openEvent(id)">
          查看纠正后的包 #{{ id }}
        </el-button>
        <h2 class="text-lg">
          {{ detail.event.title }}
        </h2>
        <div v-for="link in detail.related_events" :key="link.id">
          <el-button link @click="eventResearch?.open(link.id)">
            关联事件 #{{ link.id }}
          </el-button> {{ link.reason }}
        </div>
        <details v-if="detail.corrections?.length">
          <summary>分包纠正历史</summary><pre v-for="item in detail.corrections" :key="item.id" class="whitespace-pre-wrap break-all">{{ item.result }}</pre>
        </details>
        <details v-if="detail.grouping_versions?.length">
          <summary>Luna 分包版本</summary><p v-for="item in detail.grouping_versions" :key="item.id">
            #{{ item.id }} · {{ item.state }} {{ item.error }}
          </p>
        </details>
        <template v-if="detail.event.result">
          <p>{{ detail.event.result.summary }}</p><p><b>事实：</b>{{ detail.event.result.facts }}</p><p><b>时间线：</b>{{ detail.event.result.timeline }}</p>
          <p><b>资产影响：</b>{{ detail.event.result.asset_impacts }}</p><p><b>相关意见：</b>{{ detail.event.result.opinions }}</p>
          <p><b>不确定性：</b>{{ detail.event.result.uncertainty }}</p>
          <blockquote v-for="ref in detail.event.result.evidence_refs" :key="`${ref.article_id}:${ref.quote}`">
            来源 #{{ ref.article_id }}：{{ ref.quote }}
          </blockquote>
        </template>
        <p>{{ detail.event.result?.counter_evidence }}</p><p>关注点：{{ detail.event.result?.watchpoints }}</p>
        <div v-for="claim in detail.event.result?.claims" :key="claim.statement">
          <b>{{ claim.verdict }}</b> {{ claim.statement }}<blockquote v-for="ref in claim.evidence_refs" :key="`${ref.article_id}:${ref.quote}`">
            来源 {{ ref.article_id }}：{{ ref.quote }}
          </blockquote>
        </div>
        <div v-for="relation in detail.event.result?.related_packages" :key="relation.id">
          <el-button link @click="openEvent(relation.id)">
            相关包 #{{ relation.id }} · {{ relation.relation }}
          </el-button><p>{{ relation.reason }}</p>
        </div>
        <h3>核验工具与证据</h3><div v-for="record in detail.evidence" :key="record.id">
          <details><summary>{{ record.kind }} · {{ formatTime(record.fetched_at) }}</summary><pre class="whitespace-pre-wrap break-all">{{ record.data }}</pre></details>
        </div>
        <h3>总结版本</h3>
        <div v-for="version in detail.versions" :key="version.id" class="mb-3 border-b pb-2">
          v{{ version.revision }} · {{ version.model }} · {{ version.state }} · {{ formatTime(version.analyzed_at) }}
          <p>{{ version.error || version.result?.summary }}</p>
        </div>
        <h3>相关文章</h3><div v-for="item in detail.history" :key="item.id" class="mb-5 border-b border-slate-200 pb-4">
          <a :href="item.input.url" target="_blank" rel="noopener noreferrer">{{ item.input.title || '原始新闻' }}</a><p class="text-xs text-slate-500">
            {{ item.input.source }} · 发布 {{ formatTime(item.input.published_at) }} · 进展 {{ formatTime(item.input.progress_at) }} · 分析 {{ formatTime(item.analyzed_at) }} · {{ item.model }} {{ item.historical ? '· 补分析' : '' }}
          </p>
          <template v-if="item.result">
            <p class="text-amber-600">
              {{ starText(item.result.risk_stars) }} · {{ labels[item.result.verification] }}
            </p><p>{{ item.result.summary }}</p><p>依据：{{ item.result.reasoning }}</p><blockquote class="text-sm text-slate-600">
              {{ item.result.evidence }}
            </blockquote><p>不确定性：{{ item.result.uncertainty }}</p>
          </template>
        </div>
      </template>
    </el-drawer>
    <el-drawer v-model="showFailures" title="失败任务（最近100条）" size="min(700px, 100vw)">
      <el-select v-model="taskStage">
        <el-option label="文章初筛" value="grouping" /><el-option label="包研判" value="analysis" /><el-option label="事件时间线" value="events" /><el-option label="市场综述" value="outlook" />
      </el-select>
      <div v-for="item in failures" :key="item.id" class="mb-4">
        <b>{{ item.input.title }}</b><p>{{ item.error }}</p><el-button @click="retry(item.id)">
          重试
        </el-button>
      </div>
    </el-drawer>
  </div>
</template>
