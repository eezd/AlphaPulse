<script setup lang="ts">
import type { Config, Coverage, Event, EventDetail, Package, PackageDetail, QueueStatus, VersionDetail, Vocabulary } from "@@/apis/news-research/type"
import type { Provider } from "@@/apis/settings/type"
import type { SkillNode } from "@@/apis/skills/type"
import * as api from "@@/apis/news-research"
import { listProviderModelsApi, listProvidersApi } from "@@/apis/settings"
import { listSkillTreeApi } from "@@/apis/skills"
import AiAvailability from "@@/components/AiAvailability/index.vue"
import { EFFORTS } from "@@/constants/ai"
import { downloadText } from "@@/utils/export"
import dayjs from "dayjs"

import timezone from "dayjs/plugin/timezone"
import utc from "dayjs/plugin/utc"
import { useUserStore } from "@/pinia/stores/user"

dayjs.extend(utc)
dayjs.extend(timezone)

const vocabularyOpened = ref(false)
const vocabulary = ref<Vocabulary>({ revision: 0, entries: [] })
const vocabularyBusy = ref(false)
const vocabularyLoading = ref(false)
const vocabularyError = ref(false)
let vocabularySequence = 0
const versionOpened = ref(false)
const versionDetail = ref<VersionDetail | null>(null)
const versionError = ref(false)
let versionSequence = 0
async function openVocabulary() {
  if (!valid() || vocabularyBusy.value) return
  const seq = ++vocabularySequence
  vocabularyOpened.value = true
  vocabularyLoading.value = true
  vocabularyError.value = false
  try {
    const value = await api.getVocabularyApi()
    if (valid() && vocabularyOpened.value && seq === vocabularySequence) vocabulary.value = value
  } catch {
    if (valid() && vocabularyOpened.value && seq === vocabularySequence) vocabularyError.value = true
  } finally {
    if (valid() && seq === vocabularySequence) vocabularyLoading.value = false
  }
}
async function saveVocabulary() {
  if (!valid() || !vocabularyOpened.value || vocabularyLoading.value || vocabularyError.value || vocabularyBusy.value) return
  const seq = vocabularySequence
  vocabularyBusy.value = true
  try {
    const value = await api.saveVocabularyApi({ base_revision: vocabulary.value.revision, entries: vocabulary.value.entries })
    if (!valid() || !vocabularyOpened.value || seq !== vocabularySequence) return
    vocabulary.value = value
    vocabularyOpened.value = false
    await refresh()
  } finally {
    if (valid()) vocabularyBusy.value = false
  }
}
async function openVersion(event: number, version: number) {
  const seq = ++versionSequence
  versionDetail.value = null
  versionError.value = false
  versionOpened.value = true
  try {
    const value = await api.getEventVersionApi(event, version)
    if (valid() && versionOpened.value && seq === versionSequence) versionDetail.value = value
  } catch {
    if (valid() && versionOpened.value && seq === versionSequence) versionError.value = true
  }
}
async function openCurrentEvent() {
  const id = versionDetail.value?.event_id
  if (!id || !valid()) return
  versionOpened.value = false
  await openEvent(id)
}
watch(versionOpened, (value) => {
  if (!value) versionSequence++
})
watch(vocabularyOpened, (value) => {
  if (!value) {
    vocabularySequence++
    vocabularyLoading.value = false
  }
})
const user = useUserStore()
const route = useRoute()
const q = ref("")
const eventState = ref("")
const importance = ref(0)
watch([eventState, importance], () => {
  page.value = 1
  void refresh()
})
const tab = ref("events")
const page = ref(1)
const total = ref(0)
const events = ref<Event[]>([])
const packages = ref<Package[]>([])
const detail = ref<EventDetail | null>(null)
const eventDetailTab = ref("research")
const exportingReport = ref(false)
async function exportReport() {
  const event = detail.value
  if (!valid() || !opened.value || !event || exportingReport.value) return
  const latest = event.versions.find(version => version.state === "done" && version.result)
  if (!latest) {
    ElMessage.warning("尚无成功研究版本，无法导出报告")
    return
  }
  exportingReport.value = true
  try {
    const version = await api.getEventVersionApi(event.id, latest.id)
    if (!valid() || !opened.value || detail.value?.id !== event.id) return
    const result = version.result
    const sections = [`# ${result.title}`, `成功研究版本 #${version.id} · 研究修订 ${version.revision}`, `当前事件状态：${states[event.state] ?? event.state} · 当前事件修订 ${event.revision}`]
    if (event.revision !== version.revision || event.state !== "done") sections.push("> 本报告为上一成功研究版本。当前事件存在待研究的变化，新增或修订材料尚未由本报告核验。")
    sections.push(`## 总结\n${result.summary}`, `重要性 ${result.importance_stars}/5 · 风险 ${result.risk_stars}/5 · 可信度 ${result.confidence}`, ...Object.entries({ facts: "事实", reasoning: "判断依据", verification: "核验结论", asset_impacts: "影响", opinions: "相关意见", counter_evidence: "反证", uncertainty: "不确定性", watchpoints: "后续观察" }).map(([key, label]) => `## ${label}\n${result[key as keyof typeof result]}`), `## 时间线\n${result.timeline_points.map(p => `- ${p.time_text || "发生时间未知"}：${p.description}（新闻包 ${p.package_ids.join(", ")}）`).join("\n")}`)
    sections.push(`## 本版本新闻来源（研究时快照）\n${version.articles.map(article => `- [${article.title || `新闻 #${article.id}`}](${article.url})`).join("\n")}`)
    const included = new Set(version.packages.map(item => item.id))
    const additional = event.packages.filter(item => !included.has(item.id))
    if (additional.length) sections.push(`## 当前事件其他新闻包（未纳入本版研究）\n${additional.map(item => `- [${item.title || `新闻 #${item.news_id}`}](${item.url})`).join("\n")}`)
    downloadText(`event-${event.id}-version-${version.id}.md`, sections.join("\n\n"), "text/markdown;charset=utf-8")
  } finally {
    exportingReport.value = false
  }
}
const packageDetail = ref<PackageDetail | null>(null)
const opened = ref(false)
const packageOpened = ref(false)
const settingsOpened = ref(false)
const enabled = ref(false)
const startAt = ref<Date | null>(null)
const savedStartAt = ref<Date | null>(null)
const settingsRevision = ref(0)
const queue = ref<QueueStatus | null>(null)
const idleReasonLabels: Record<string, string> = { paused: "已暂停", draining: "等待已接收任务收尾", assignment_serial: "事件归属串行处理中", preceding_index: "等待前序新闻完成索引", waiting_previous: "等待上一轮研究完成", aggregation: "等待事件聚合窗口", dependencies: "等待事件成员处理完成", manual_retry: "部分任务需人工重试", no_work: "暂无可执行任务", dispatching: "正在补入任务" }
function disabledBackfillDate(date: Date) {
  return dayjs.tz(dayjs(date).format("YYYY-MM-DD"), "Asia/Shanghai").valueOf() > Date.now()
}
const startText = computed({
  get: () => startAt.value ? dayjs(startAt.value).tz("Asia/Shanghai").format("YYYY-MM-DD HH:mm:ss") : "",
  set: (value: string) => {
    startAt.value = value ? dayjs.tz(value, "Asia/Shanghai").toDate() : null
  }
})
const selectedStartAt = computed({
  get: () => enabled.value && !settingsOpened.value ? savedStartAt.value : startAt.value,
  set: (value: Date | null) => {
    startAt.value = value
  }
})
const coverage = ref<Coverage | null>(null)
const coverageLoading = ref(false)
const coverageError = ref(false)
let coverageSequence = 0
const loading = ref(false)
const error = ref(false)
const busy = ref(false)
const providers = ref<Provider[]>([])
const skills = ref<SkillNode[]>([])
const newsIds = ref("")
const config = ref<Config>({ index: { provider_id: null, model: "", reasoning_effort: null, skill_id: null }, research: { provider_id: null, model: "", reasoning_effort: null, skill_id: null }, concurrency: 3 })
const modelOptions = ref<Record<string, string[]>>({ index: [], research: [] })
const modelErrors = ref<Record<string, string>>({ index: "", research: "" })
const modelSequences: Record<string, number> = { index: 0, research: 0 }
const detailError = ref(false)
const packageError = ref(false)
let active = true
let sequence = 0
let detailSequence = 0
let packageSequence = 0
let eventId: number | undefined
let packageId: number | undefined
let detailLoading = false
let packageLoading = false
let timer: ReturnType<typeof setInterval> | undefined
const owner = user.sessionVersion
const valid = () => active && owner === user.sessionVersion
const states: Record<string, string> = { index_pending: "等待关键词索引", index_processing: "生成关键词", index_failed: "索引失败", assign_pending: "等待事件分类", assign_processing: "事件分类中", assign_failed: "分类失败", done: "已完成", pending: "等待研究", processing: "研究中", failed: "研究失败", retired: "已归档" }
async function load() {
  if (!valid()) return
  const seq = ++sequence
  loading.value = true
  try {
    const [settings, rows] = await Promise.all([api.getSettingsApi(), tab.value === "events" ? api.listEventsApi(q.value, page.value, { state: eventState.value || undefined, importance_min: importance.value }) : api.listPackagesApi(q.value, page.value)])
    if (!valid() || seq !== sequence) return
    enabled.value = settings.enabled
    queue.value = settings.queue ?? null
    if (!startAt.value || startAt.value.getTime() === savedStartAt.value?.getTime()) startAt.value = new Date(settings.start_at)
    savedStartAt.value = new Date(settings.start_at)
    if (tab.value === "events") events.value = rows.items as Event[]
    else packages.value = rows.items as Package[]
    total.value = rows.total
    error.value = false
    await checkCoverage()
  } catch {
    if (valid() && seq === sequence) error.value = true
  } finally {
    if (valid() && seq === sequence) loading.value = false
  }
}
async function checkCoverage() {
  if (!valid() || !selectedStartAt.value || !Number.isFinite(selectedStartAt.value.getTime())) return
  const start = selectedStartAt.value.toISOString()
  const seq = ++coverageSequence
  coverageLoading.value = true
  coverageError.value = false
  coverage.value = null
  try {
    const result = await api.getCoverageApi(start)
    if (valid() && seq === coverageSequence && selectedStartAt.value?.toISOString() === start) coverage.value = result
  } catch {
    if (valid() && seq === coverageSequence) coverageError.value = true
  } finally {
    if (valid() && seq === coverageSequence) coverageLoading.value = false
  }
}
async function enable() {
  if (!valid()) return
  await api.enableApi()
}
async function openEvent(id: number) {
  eventId = id
  detailSequence++
  detailLoading = false
  detail.value = null
  detailError.value = false
  opened.value = true
  await refreshEvent()
}
async function openPackage(id: number) {
  packageId = id
  packageSequence++
  packageLoading = false
  packageDetail.value = null
  packageError.value = false
  packageOpened.value = true
  await refreshPackage()
}
async function refreshEvent() {
  const id = eventId
  if (!valid() || !opened.value || !id || detailLoading) return
  detailLoading = true
  const seq = ++detailSequence
  try {
    const value = await api.getEventApi(id)
    if (valid() && opened.value && eventId === id && seq === detailSequence) {
      detail.value = value
      detailError.value = false
    }
  } catch {
    if (valid() && opened.value && eventId === id && seq === detailSequence) detailError.value = true
  } finally {
    if (seq === detailSequence) detailLoading = false
  }
}
async function refreshPackage() {
  const id = packageId
  if (!valid() || !packageOpened.value || !id || packageLoading) return
  packageLoading = true
  const seq = ++packageSequence
  try {
    const value = await api.getPackageApi(id)
    if (valid() && packageOpened.value && packageId === id && seq === packageSequence) {
      packageDetail.value = value
      packageError.value = false
    }
  } catch {
    if (valid() && packageOpened.value && packageId === id && seq === packageSequence) packageError.value = true
  } finally {
    if (seq === packageSequence) packageLoading = false
  }
}
async function refresh() {
  await Promise.all([load(), refreshEvent(), refreshPackage()])
}
async function loadModels(stage: "index" | "research") {
  const seq = ++modelSequences[stage]
  const id = config.value[stage].provider_id
  modelOptions.value[stage] = []
  modelErrors.value[stage] = ""
  if (!id || !valid() || !settingsOpened.value) return
  try {
    const rows = await listProviderModelsApi(id)
    if (valid() && seq === modelSequences[stage] && settingsOpened.value) modelOptions.value[stage] = rows
  } catch {
    if (valid() && seq === modelSequences[stage] && settingsOpened.value) modelErrors.value[stage] = "模型列表读取失败，可手动填写模型名称"
  }
}
async function settings() {
  const [value, choices, nodes] = await Promise.all([api.getSettingsApi(), listProvidersApi(), listSkillTreeApi()])
  if (!valid()) return
  providers.value = choices
  skills.value = nodes.filter(s => s.scope === "news" && s.kind === "skill")
  if (value.config.index && value.config.research) config.value = value.config as Config
  startAt.value = new Date(value.start_at)
  settingsRevision.value = value.control_revision
  settingsOpened.value = true
}
async function action(fn: () => Promise<unknown>) {
  if (!valid() || busy.value) return
  busy.value = true
  try {
    await fn()
    if (valid()) {
      detailSequence++
      packageSequence++
      detailLoading = false
      packageLoading = false
      await refresh()
    }
  } finally {
    if (valid()) busy.value = false
  }
}
async function retryEvent() {
  const id = detail.value?.id
  if (id) await action(() => api.retryEventApi(id))
}
async function retryPackage() {
  const id = packageDetail.value?.id
  if (id) await action(() => api.retryPackageApi(id))
}
async function save() {
  if (!valid() || busy.value) return
  if (!startAt.value || !Number.isFinite(startAt.value.getTime()) || startAt.value.getTime() > Date.now()) {
    ElMessage.error("请选择不晚于当前时间的回补起始时间")
    return
  }
  await action(() => api.saveSettingsApi({ ...config.value, start_at: startAt.value!.toISOString(), base_revision: settingsRevision.value }))
  if (valid()) settingsOpened.value = false
}
async function prepareSkills() {
  const ids = await api.createSkillsApi()
  if (!valid()) return
  config.value.index.skill_id = ids.index
  config.value.research.skill_id = ids.research
  const nodes = await listSkillTreeApi()
  if (!valid()) return
  skills.value = nodes.filter(s => s.scope === "news" && s.kind === "skill")
  if (valid()) ElMessage.success("已生成草稿，请在 Skills 页面审阅并发布")
}
async function enroll() {
  const ids = newsIds.value.split(/[\s,，]+/).filter(Boolean).map(Number)
  if (!ids.length || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) {
    ElMessage.error("请输入有效的新闻 ID")
    return
  }
  await action(() => api.enrollApi(ids))
}
for (const stage of ["index", "research"] as const) {
  watch([settingsOpened, () => config.value[stage].provider_id], () => void loadModels(stage))
}
watch(opened, (value) => {
  if (!value) detailSequence++
})
watch(packageOpened, (value) => {
  if (!value) packageSequence++
})
watch(selectedStartAt, () => {
  coverageSequence++
  coverage.value = null
  coverageLoading.value = false
  coverageError.value = false
}, { flush: "sync" })
watch(() => user.sessionVersion, () => {
  sequence++
  versionSequence++
  versionOpened.value = false
  vocabularyOpened.value = false
  versionDetail.value = null
  detailSequence++
  packageSequence++
  events.value = []
  packages.value = []
  detail.value = null
  packageDetail.value = null
  opened.value = false
  packageOpened.value = false
  settingsOpened.value = false
  startAt.value = null
  savedStartAt.value = null
})
watch([tab, q], () => {
  page.value = 1
  void load()
})
watch(page, () => void load())
onMounted(() => {
  void load()
  if (route.query.event) void openEvent(Number(route.query.event))
  if (route.query.package) void openPackage(Number(route.query.package))
  timer = setInterval(() => {
    if (!document.hidden && !loading.value) void refresh()
  }, 15000)
})
onBeforeUnmount(() => {
  active = false
  sequence++
  detailSequence++
  packageSequence++
  coverageSequence++
  clearInterval(timer)
})
</script>

<template>
  <div class="research-workspace">
    <AiAvailability />
    <section class="research-controls">
      <div class="research-control-row">
        <el-tag :type="enabled ? 'success' : 'info'">
          ● {{ enabled ? '处理中' : '已暂停' }}
        </el-tag>
        <div v-if="queue" class="stage-meters">
          <div v-for="(label, stage) in { index: '关键词提取', assign: '事件归属', research: '事件研究' }" :key="stage" class="stage-meter" :class="{ 'stage-ai': stage === 'research' }">
            <span>{{ label }}</span><b>{{ queue.running_by_stage[stage] }} / {{ queue.capacity }}</b>
            <div class="pipeline-track">
              <i :style="{ width: `${queue.capacity ? queue.running_by_stage[stage] / queue.capacity * 100 : 0}%` }" />
            </div>
          </div>
        </div>
        <span class="research-boundary">AI 回补起始时间<b>{{ savedStartAt ? dayjs(savedStartAt).tz("Asia/Shanghai").format("YYYY-MM-DD HH:mm:ss") : '—' }}</b><small>北京时间 · 实时任务优先</small></span>
        <el-button :loading="busy" :disabled="!enabled && (loading || error || queue?.draining)" @click="action(enabled ? api.pauseApi : enable)">
          {{ enabled ? '暂停新任务' : '启用两阶段处理' }}
        </el-button>
        <el-button class="ai-settings-button" @click="settings">
          模型与 Skills
        </el-button>
      </div>
      <div class="research-control-row">
        <template v-if="queue">
          <el-tag>实时待索引 {{ (queue.realtime.missing ?? 0) + (queue.realtime.stale ?? 0) + (queue.realtime.index_pending ?? 0) }} · 待归属 {{ queue.realtime.assign_pending ?? 0 }}</el-tag>
          <el-tag type="info">
            回补待索引 {{ (queue.backfill.missing ?? 0) + (queue.backfill.stale ?? 0) + (queue.backfill.index_pending ?? 0) }} · 待归属 {{ queue.backfill.assign_pending ?? 0 }}
          </el-tag>
          <el-tag :type="queue.waiting_retry ? 'warning' : 'info'">
            等待重试 {{ queue.waiting_retry ?? 0 }}
          </el-tag>
          <el-popover title="动态队列" width="360" trigger="hover">
            <template #reference>
              <el-button link>
                共享并发 {{ queue.running }} / {{ queue.capacity }} ⓘ
              </el-button>
            </template><p>实时就绪研究 {{ queue.events.realtime.ready ?? 0 }} · 回补就绪研究 {{ queue.events.backfill.ready ?? 0 }}</p><p>{{ queue.idle_reasons.map(reason => idleReasonLabels[reason] ?? reason).join('；') || '持续补入任务' }}</p><p>任务完成后持续补位；事件归属串行，其他任务共享剩余并发。等待重试不代表正在调用模型。</p>
          </el-popover>
        </template>
        <div class="flex-1" />
        <el-button @click="openVocabulary">
          短语词库
        </el-button>
        <el-button :loading="loading" @click="refresh">
          刷新
        </el-button>
        <el-input v-model="newsIds" placeholder="输入新闻 ID，以逗号分隔" class="w-56">
          <template #append>
            <el-button :loading="busy" @click="enroll">
              入队
            </el-button>
          </template>
        </el-input>
      </div>
      <p v-if="queue?.draining" class="text-amber-700 text-xs">
        正在完成 {{ queue.running }} 个已接收调用，收尾后可重新启用。
      </p>
    </section>
    <el-alert v-if="error" title="读取失败，保留上次结果，可刷新重试" type="error" :closable="false" />
    <section class="research-results">
      <div class="research-filter-row">
        <h1>新闻与事件研究</h1><div class="flex-1" />
        <el-input v-model="q" clearable placeholder="搜索名称、规范关键词或原文别名" class="w-80" />
        <el-select v-if="tab === 'events'" v-model="eventState" clearable placeholder="全部研究状态" class="w-36">
          <el-option v-for="(label, state) in { pending: '待研究', processing: '研究中', done: '研究完成', failed: '研究失败' }" :key="state" :label="label" :value="state" />
        </el-select>
        <el-select v-if="tab === 'events'" v-model="importance" class="w-36">
          <el-option label="全部重要性" :value="0" /><el-option label="四星及以上" :value="4" /><el-option label="五星" :value="5" />
        </el-select>
      </div>
      <el-tabs v-model="tab">
        <el-tab-pane label="事件" name="events">
          <el-card v-for="event in events" :key="event.id" class="event-card mb-3" shadow="never">
            <el-button class="event-open-button" type="primary" @click="openEvent(event.id)">
              详情抽屉
            </el-button>
            <el-button class="event-title" link @click="openEvent(event.id)">
              {{ event.title }}
            </el-button>
            <el-tag class="ml-2">
              {{ states[event.state] ?? event.state }}
            </el-tag>
            <span class="ml-3 text-xs text-slate-500">{{ event.package_count ?? 0 }} 个新闻包 · {{ event.source_count ?? 0 }} 个来源</span>
            <template v-if="event.result">
              <p class="my-2">
                {{ event.result.summary }}
              </p>
              <div class="event-rating">
                <el-tag type="danger">
                  重要性 {{ "★".repeat(event.result.importance_stars) }}{{ "☆".repeat(5 - event.result.importance_stars) }}
                </el-tag><el-tag type="info">
                  风险 {{ event.result.risk_stars }}/5
                </el-tag><el-tag type="success">
                  可信度 {{ event.result.confidence }}
                </el-tag>
              </div>
            </template>
            <p v-if="event.error" class="text-red-600">
              {{ event.error }}
            </p>
            <p v-if="event.result && event.state !== 'done'" class="text-sm text-slate-500">
              显示上一成功研判，等待本次更新。
            </p>
          </el-card>
          <el-empty v-if="!events.length && !loading" description="暂无事件，新闻完成关键词索引与分类后会出现在这里" />
        </el-tab-pane>
        <el-tab-pane label="新闻包与关键词" name="packages">
          <el-card v-for="item in packages" :key="item.id" class="mb-3" shadow="never">
            <el-button link @click="openPackage(item.id)">
              {{ item.title || `新闻 #${item.news_id}` }}
            </el-button>
            <span class="ml-2 text-sm">{{ states[item.state] ?? item.state }}</span>
            <div class="my-2 flex flex-wrap gap-2">
              <el-tag v-for="word in item.keywords" :key="word.canonical" :title="word.aliases.join(' / ')">
                {{ word.canonical }}
              </el-tag>
            </div>
            <el-button v-for="event in item.events" :key="event.id" link @click="openEvent(event.id)">
              {{ event.title }}
            </el-button>
            <p v-if="item.error" class="text-red-600">
              {{ item.error }}
            </p>
          </el-card>
          <el-empty v-if="!packages.length && !loading" description="暂无新闻包" />
        </el-tab-pane>
      </el-tabs>
      <el-pagination v-model:current-page="page" :total="total" :page-size="20" layout="prev, pager, next, total" />
    </section>
    <el-drawer v-model="opened" title="事件研究与时间线" size="min(820px, 100vw)">
      <el-alert v-if="detailError" title="事件详情读取失败，请重新打开重试" type="error" :closable="false" />
      <template v-if="detail">
        <p v-if="detail.wait_reason" class="mb-3 text-amber-700">
          {{ detail.wait_reason }}
        </p>
        <h2>{{ detail.title }}</h2><p>{{ states[detail.state] ?? detail.state }}</p>
        <el-button :loading="exportingReport" :disabled="!detail.versions.some(version => version.state === 'done' && version.result)" @click="exportReport">
          导出研究报告
        </el-button>
        <el-button :loading="busy" @click="retryEvent">
          重新研究
        </el-button>
        <el-alert v-if="detail.error" :title="detail.error" type="warning" :closable="false" />
        <el-tabs v-model="eventDetailTab">
          <el-tab-pane label="研究结论" name="research">
            <template v-if="detail.result">
              <p class="my-3">
                {{ detail.result.summary }}
              </p>
              <p>重要性 {{ detail.result.importance_stars }}/5 · 风险 {{ detail.result.risk_stars }}/5 · 可信度 {{ detail.result.confidence }}</p>
              <p v-for="(label, key) in { facts: '事实', reasoning: '判断依据', verification: '核验结论', asset_impacts: '影响', opinions: '相关意见', counter_evidence: '反证', uncertainty: '不确定性', watchpoints: '后续观察' }" :key="key" class="my-3">
                {{ label }}：{{ detail.result[key] }}
              </p>
            </template>
          </el-tab-pane>
          <el-tab-pane label="主体时间线" name="timeline">
            <template v-if="detail.result">
              <div v-for="(point, i) in detail.result.timeline_points" :key="i" class="my-4 border-l-2 pl-3">
                <b>{{ point.time_text || '发生时间未知' }}</b><p>{{ point.description }}</p>
                <p v-if="point.planned_at">
                  计划：{{ point.planned_precision === 'day' ? point.planned_at.slice(0, 10) : point.planned_precision === 'month' ? point.planned_at.slice(0, 7) : point.planned_at }}
                </p>
                <p v-if="point.period_text">
                  统计期：{{ point.period_text }}
                </p>
                <p v-if="point.reported_at">
                  报道时间：{{ point.reported_at }}
                </p>
                <el-button v-for="id in point.package_ids" :key="id" link @click="openPackage(id)">
                  新闻包 #{{ id }}
                </el-button>
                <blockquote v-for="(ref, j) in point.evidence_refs" :key="j" class="my-2 text-slate-500">
                  {{ ref.quote }}
                </blockquote>
              </div>
            </template><el-empty v-else description="尚无时间线" />
          </el-tab-pane>
          <el-tab-pane label="证据与新闻包" name="evidence">
            <h3 class="mt-4">
              相关事件引用
            </h3>
            <div v-for="ref in detail.references" :key="ref.version_id" class="my-2">
              <el-button link @click="openVersion(ref.event_id, ref.version_id)">
                {{ ref.title }} · 版本 #{{ ref.version_id }}
              </el-button>
              <p>{{ ref.role }} · {{ ref.reason }}</p><el-tag v-if="ref.changed" type="warning">
                引用来源有变化，待下次研究核对
              </el-tag>
            </div>
            <h3 class="mt-4">
              全部新闻包
            </h3>
            <div v-for="item in detail.packages" :key="item.id">
              <el-button link @click="openPackage(item.id)">
                {{ item.title || `新闻 #${item.news_id}` }}
              </el-button>
              <p class="text-xs text-slate-500">
                {{ item.source_code }} · {{ item.published_at ? dayjs(item.published_at).tz("Asia/Shanghai").format("YYYY-MM-DD HH:mm") : "" }} <a :href="item.url" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>
              </p>
            </div>
          </el-tab-pane>
          <el-tab-pane label="版本历史" name="versions">
            <h3 class="mt-4">
              研判版本
            </h3>
            <details v-for="version in detail.versions" :key="version.id" class="my-3">
              <summary>#{{ version.id }} · {{ version.state }} · {{ version.error }}</summary>
              <p>{{ version.result?.summary }}</p><pre class="whitespace-pre-wrap break-all">{{ version.result }}</pre>
              <details>
                <summary>核验记录与用量</summary><pre class="whitespace-pre-wrap break-all">{{ version.evidence }}
                {{ version.usage }}</pre>
              </details>
            </details>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>
    <el-drawer v-model="packageOpened" title="新闻原文与关键词" size="min(760px, 100vw)">
      <el-alert v-if="packageError" title="新闻包读取失败，请重新打开重试" type="error" :closable="false" />
      <template v-if="packageDetail">
        <h2>{{ packageDetail.title }}</h2><p>{{ states[packageDetail.state] ?? packageDetail.state }}</p>
        <el-button :loading="busy" @click="retryPackage">
          重试或重新归属事件
        </el-button>
        <p v-if="packageDetail.error" class="text-red-600">
          {{ packageDetail.error }}
        </p>
        <div v-for="word in packageDetail.keywords" :key="word.canonical" class="my-3">
          <b>{{ word.canonical }}</b> · {{ word.aliases.join(' / ') }}<blockquote>{{ word.quote }}</blockquote>
        </div>
        <el-button v-for="event in packageDetail.events" :key="event.id" link @click="openEvent(event.id)">
          {{ event.title }}
        </el-button>
        <p class="my-3 whitespace-pre-wrap">
          {{ packageDetail.article.summary }}
        </p><article class="whitespace-pre-wrap">
          {{ packageDetail.article.content }}
        </article>
      </template>
    </el-drawer>
    <el-drawer v-model="vocabularyOpened" title="短语词库" size="min(720px, 100vw)">
      <p class="mb-3">
        将同一主体的名称归为一个规范词。保存后用于索引归一和检索，不自动重新研究事件。
      </p>
      <p v-if="vocabularyLoading">
        正在读取词库…
      </p>
      <el-alert v-else-if="vocabularyError" title="词库读取失败，请重新打开重试" type="error" :closable="false" />
      <template v-else>
        <div v-for="(entry, i) in vocabulary.entries" :key="i" class="mb-4">
          <el-input v-model="entry.canonical" :disabled="vocabularyBusy" placeholder="规范词，例如：币安" />
          <el-select v-model="entry.aliases" :disabled="vocabularyBusy" multiple filterable allow-create default-first-option placeholder="输入别名并回车，例如：Binance" class="my-2 w-full" />
          <el-button :disabled="vocabularyBusy" @click="vocabulary.entries.splice(i, 1)">
            删除词条
          </el-button>
        </div>
        <el-button :disabled="vocabularyBusy" @click="vocabulary.entries.push({ canonical: '', aliases: [] })">
          新增词条
        </el-button>
        <el-button type="primary" :loading="vocabularyBusy" @click="saveVocabulary">
          保存词库
        </el-button>
      </template>
    </el-drawer>
    <el-drawer v-model="versionOpened" title="被引用的固定版本" size="min(820px, 100vw)">
      <el-alert v-if="versionError" title="版本读取失败，请重新打开" type="error" :closable="false" />
      <template v-if="versionDetail">
        <h2>{{ versionDetail.result.title }} · 版本 #{{ versionDetail.id }}</h2>
        <el-button link @click="openCurrentEvent">
          查看当前事件
        </el-button>
        <p class="my-3">
          {{ versionDetail.result.summary }}
        </p>
        <p>重要性 {{ versionDetail.result.importance_stars }}/5 · 风险 {{ versionDetail.result.risk_stars }}/5 · 可信度 {{ versionDetail.result.confidence }}</p>
        <p v-for="(label, key) in { facts: '事实', reasoning: '判断依据', verification: '核验结论', asset_impacts: '影响', opinions: '相关意见', counter_evidence: '反证', uncertainty: '不确定性', watchpoints: '后续观察' }" :key="key" class="my-3">
          {{ label }}：{{ versionDetail.result[key] }}
        </p>
        <div v-for="(point, i) in versionDetail.result.timeline_points" :key="i" class="my-3">
          <b>{{ point.time_text || '发生时间未知' }}</b><p>{{ point.description }}</p>
          <blockquote v-for="(ref, j) in point.evidence_refs" :key="j">
            {{ ref.quote }}
          </blockquote>
        </div>
        <div v-for="ref in versionDetail.references" :key="ref.version_id">
          <el-button link @click="openVersion(ref.event_id, ref.version_id)">
            {{ ref.title }} · #{{ ref.version_id }}
          </el-button>
          <p>{{ ref.role }} · {{ ref.reason }} {{ ref.changed ? '引用来源有变化' : '' }}</p>
        </div>
        <details class="my-3">
          <summary>核验记录与用量</summary>
          <pre class="whitespace-pre-wrap break-all">{{ versionDetail.evidence }}
          {{ versionDetail.usage }}</pre>
        </details>
        <details v-for="article in versionDetail.articles" :key="article.id" class="my-3">
          <summary>原文快照：{{ article.title || article.id }}</summary>
          <p class="whitespace-pre-wrap">
            {{ article.summary }}
            {{ article.content }}
          </p>
        </details>
      </template>
    </el-drawer>
    <el-drawer v-model="settingsOpened" title="两阶段模型与 Skills" size="min(640px, 100vw)">
      <el-form label-position="top">
        <div class="mb-4 rounded border border-slate-200 p-3">
          <div class="flex flex-wrap items-center gap-3">
            <label for="research-start-at">回补起始时间</label>
            <el-date-picker
              id="research-start-at"
              v-model="startText"
              value-format="YYYY-MM-DD HH:mm:ss"
              type="datetime"
              :clearable="false"
              :disabled="busy"
              :disabled-date="disabledBackfillDate"
              placeholder="选择回补起始时间"
            />
            <span class="text-sm text-slate-500">北京时间（系统时区 UTC+8）</span>
            <el-button :loading="coverageLoading" :disabled="!startAt || busy" @click="checkCoverage">
              核查遗漏
            </el-button>
          </div>
          <p class="mt-2 text-sm text-slate-500">
            点击“保存并暂停”后生效；重新启用时重排未完成任务。实时新闻优先插队，历史新闻持续补位。
          </p>
          <p v-if="coverage" class="mt-2 text-sm">
            范围内 {{ coverage.total }} 条，全部阶段已完成 {{ coverage.completed }} 条，未完成 {{ coverage.unprocessed }} 条：
            未入队 {{ coverage.missing }}，原文更新需重判 {{ coverage.stale }}，待处理 {{ coverage.pending }}，处理中 {{ coverage.processing }}，失败 {{ coverage.failed }}。
            失败项需手动重试，范围内的遗漏会自动发现并持续加入处理。
          </p>
          <p v-if="coverage && coverage.outside.unprocessed" class="mt-2 text-sm text-amber-700">
            范围外已暂停自动处理的新闻 {{ coverage.outside.unprocessed }} 条：
            原文更新需重判 {{ coverage.outside.stale }}，待处理 {{ coverage.outside.pending }}，处理中 {{ coverage.outside.processing }}，失败 {{ coverage.outside.failed }}。
            已接收调用允许完成，扩大范围后恢复尚未完成的阶段；失败项需手动重试。
          </p>
          <p v-if="coverageError" class="mt-2 text-sm text-red-600">
            核查失败，请重试。
          </p>
        </div>

        <template v-for="stage in (['index', 'research'] as const)" :key="stage">
          <h3>{{ stage === 'index' ? '第一阶段：关键词索引与事件归属' : '第二阶段：事件整体研究' }}</h3>
          <el-form-item label="模型接入">
            <el-select v-model="config[stage].provider_id" class="w-full">
              <el-option v-for="provider in providers" :key="provider.id" :label="provider.name" :value="provider.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="模型名称">
            <el-select v-model="config[stage].model" filterable allow-create default-first-option class="w-full">
              <el-option v-for="name in modelOptions[stage]" :key="name" :label="name" :value="name" />
            </el-select><p v-if="modelErrors[stage]">
              {{ modelErrors[stage] }}
            </p>
          </el-form-item>
          <el-form-item label="思考强度">
            <el-select v-model="config[stage].reasoning_effort" clearable :value-on-clear="null" placeholder="沿用接入配置">
              <el-option v-for="effort in EFFORTS" :key="effort" :label="effort" :value="effort" />
            </el-select>
          </el-form-item>
          <el-form-item label="已发布的新闻 Skill">
            <el-select v-model="config[stage].skill_id" class="w-full">
              <el-option v-for="skill in skills" :key="skill.id" :value="skill.id" :label="`${skill.path} · v${skill.published_version_no ?? '未发布'}`" />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item label="共享并发">
          <el-input-number v-model="config.concurrency" :min="1" :max="16" />
        </el-form-item>
        <el-button @click="prepareSkills">
          生成或更新默认 Skill 草稿
        </el-button><router-link to="/skills">
          审阅并发布 Skills
        </router-link>
        <p class="my-3 text-sm text-slate-500">
          保存设置会暂停新任务。第一阶段先保存关键词，再分类；分类重试不会重新提取关键词。
          第一阶段 Skill 必须包含“## 索引规则”和“## 归属规则”两个分段，每次调用只读取对应规则。
        </p>
        <el-button type="primary" :loading="busy" @click="save">
          保存并暂停
        </el-button>
      </el-form>
    </el-drawer>
  </div>
</template>
