<script setup lang="ts">
import type * as Risk from "@/common/apis/risk/type"
import type { Provider } from "@/common/apis/settings/type"
import { formatTime } from "@@/utils/text"
import * as api from "@/common/apis/risk"
import { listProviderModelsApi, listProvidersApi } from "@/common/apis/settings"
import { useUserStore } from "@/pinia/stores/user"

const user = useUserStore()
const hours = ref(24)
const stars = ref(1)
const archive = ref(false)
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
let sequence = 0
let detailSequence = 0
let modelsSequence = 0
let timer: ReturnType<typeof setInterval> | undefined
const labels: Record<string, string> = { bullish: "偏多", bearish: "偏空", mixed: "震荡／分歧", neutral: "中性", unknown: "无法判断", official: "官方公告", reported: "媒体报道", unverified: "待核实", refuted: "更正／辟谣", insufficient: "信息不足", low: "低", medium: "中", high: "高" }
const starText = (value: number) => value ? "★".repeat(value) : "无明显风险"
const levels = ["无明显风险", "关注", "警惕", "高风险", "严重", "极端系统性风险"]
async function load() {
  const seq = ++sequence
  const version = user.sessionVersion
  loading.value = true
  try {
    const [summary, list] = await Promise.all([api.getRiskOverviewApi(hours.value), api.listRiskEventsApi({ hours: archive.value ? undefined : hours.value, stars: stars.value, q: q.value, page: page.value, start: archive.value ? range.value?.[0].toISOString() : undefined, end: archive.value ? range.value?.[1].toISOString() : undefined })])
    if (seq !== sequence || version !== user.sessionVersion) return
    overview.value = summary
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
  const [config, choices] = await Promise.all([api.getRiskSettingsApi(), listProvidersApi()])
  if (version !== user.sessionVersion) return
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
  const data = await api.listRiskAnalysesApi({ failed: true })
  if (version !== user.sessionVersion) return
  failures.value = data
  showFailures.value = true
}
async function retry(id: number) {
  const version = user.sessionVersion
  await api.retryRiskAnalysisApi(id)
  if (version === user.sessionVersion) await openFailures()
}
function search() {
  if (page.value === 1) void load()
  else page.value = 1
}
watch([hours, stars, archive, range], search)
watch(page, () => void load())
watch([showSettings, () => settings.value?.provider_id], () => void loadModels())
watch(() => user.sessionVersion, () => {
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
  timer = setInterval(() => {
    if (!document.hidden && !loading.value) void load()
  }, 30000)
})
onBeforeUnmount(() => {
  sequence++
  detailSequence++
  modelsSequence++
  clearInterval(timer)
})
</script>

<template>
  <div class="mx-auto max-w-7xl flex flex-col gap-4">
    <div class="flex flex-wrap items-center gap-3">
      <div class="flex-1">
        <h1 class="m-0 text-xl font-semibold">
          新闻风险观察
        </h1><p class="mb-0 mt-1 text-sm text-slate-500">
          加密市场事件、证据与条件情景
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
        </el-radio-button><el-radio-button :value="168">
          7 天
        </el-radio-button>
      </el-radio-group>
      <span class="text-xs text-slate-500">分析起点：2026-10-26 00:00 北京时间</span>
    </div>
    <el-card v-if="overview" shadow="never">
      <div class="flex flex-wrap gap-5">
        <div v-for="level in [5, 4, 3, 2, 1]" :key="level">
          <div class="text-amber-600">
            <span :title="levels[level]">{{ starText(level) }}</span>
          </div><strong class="text-2xl">{{ overview.counts[String(level)] ?? 0 }}</strong><span class="ml-1 text-sm text-slate-500">事件</span>
        </div>
      </div>
      <p class="mb-0 text-sm text-slate-500">
        已分析 {{ overview.coverage.done }} / {{ overview.coverage.total }} 条 · 待处理 {{ overview.coverage.pending }} · 处理中 {{ overview.coverage.processing }} · <el-button link type="danger" @click="openFailures">
          失败 {{ overview.coverage.failed }}
        </el-button>
      </p>
      <el-alert v-if="overview.coverage.done < overview.coverage.total" class="mt-3" type="info" :closable="false" title="分析覆盖尚未完整；没有风险事件不代表没有风险。" />
      <p class="mb-0 text-xs text-slate-500">
        Worker 心跳：{{ formatTime(overview.heartbeat) }} · 数据读取：{{ formatTime(overview.as_of) }}
      </p>
    </el-card>
    <el-card shadow="never">
      <template #header>
        <div class="flex justify-between">
          <span>AI 综合判断 · 未来数小时至 24 小时</span><el-tag v-if="overview?.stale" type="warning">
            尚无最新判断
          </el-tag>
        </div>
      </template>
      <template v-if="overview?.scenario">
        <el-tag>{{ labels[overview.scenario.direction] }}</el-tag><p class="whitespace-pre-wrap">
          {{ overview.scenario.summary }}
        </p>
        <p><b>触发条件：</b>{{ overview.scenario.conditions }}</p><p><b>反向因素：</b>{{ overview.scenario.counter_factors }}</p><p><b>不确定性：</b>{{ overview.scenario.uncertainty }}</p>
        <el-button v-for="id in overview.scenario.event_ids" :key="id" link @click="openEvent(id)">
          依据事件 #{{ id }}
        </el-button>
        <p class="text-xs text-slate-500">
          生成于 {{ formatTime(overview.snapshot_at) }}，历史补分析不代表当时预测。
        </p>
      </template><el-empty v-else description="尚无综合判断，请配置模型并启用分析" />
      <div v-if="overview?.quotes" class="flex flex-wrap gap-4 text-sm text-slate-600">
        <span v-for="symbol in (['BTCUSDT', 'ETHUSDT'] as const)" :key="symbol">{{ symbol }}：{{ overview.quotes[symbol]?.lastPrice ?? '不可用' }} · 24h {{ overview.quotes[symbol]?.priceChangePercent ?? '-' }}%</span><span>快照 {{ formatTime(overview.quotes.at) }}</span>
      </div>
      <el-alert v-if="overview?.error" class="mt-3" type="warning" :closable="false" :title="overview.error" />
    </el-card>
    <div class="flex flex-wrap gap-3">
      <el-switch v-model="archive" active-text="历史归档" />
      <el-select v-model="stars" class="w-40">
        <el-option :value="0" label="全部评级" /><el-option v-for="n in 5" :key="n" :value="n" :label="`${n} 星及以上`" />
      </el-select>
      <el-input v-model="q" class="w-60" clearable placeholder="事件关键词" @keyup.enter="search" @clear="search" />
      <el-date-picker v-if="archive" v-model="range" type="datetimerange" start-placeholder="开始" end-placeholder="结束" />
    </div>
    <div v-loading="loading" class="flex flex-col gap-3">
      <el-card v-for="event in events" :key="event.id" shadow="never" class="cursor-pointer" @click="openEvent(event.id)">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-amber-600">{{ event.result.verification === 'insufficient' ? '信息不足，无法判断' : starText(event.stars) }}</span><el-tag size="small">
            {{ labels[event.result.verification] }}
          </el-tag><el-tag size="small" type="info">
            {{ labels[event.result.direction] }}
          </el-tag><span class="ml-auto text-xs text-slate-500">最新进展 {{ formatTime(event.updated_at) }}</span>
        </div>
        <h3 class="my-2 text-base">
          {{ event.title }}
        </h3><p class="my-2 text-sm text-slate-600">
          {{ event.result.summary }}
        </p><p class="mb-0 text-xs text-slate-500">
          {{ event.entities.join(' · ') }} · 历史最高 {{ starText(event.peak_stars) }}
        </p>
      </el-card>
      <el-empty v-if="!events.length && !loading" description="该范围内暂无已归档的风险事件" />
      <el-pagination v-model:current-page="page" :total="total" :page-size="20" layout="prev, pager, next, total" />
    </div>
    <el-dialog v-model="showSettings" title="新闻 AI 分析设置" width="min(600px, 95vw)">
      <el-form v-if="settings" label-position="top">
        <el-form-item label="API 接入配置（复用地址、协议和密钥）">
          <el-select v-model="settings.provider_id" class="w-full">
            <el-option v-for="provider in providers" :key="provider.id" :value="provider.id" :label="provider.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="新闻模型名称">
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
        <el-alert type="info" :closable="false" title="测试并启用会调用模型，随后分析起点之后的全部新闻；不会修改 Chat 或交易模型。" />
        <p>当前状态：{{ settings.enabled ? '已启用' : '已关闭' }}</p>
        <el-button :loading="saving" @click="save(false)">
          保存并暂停分析
        </el-button><el-button type="primary" :loading="saving" @click="save(true)">
          测试并启用
        </el-button>
      </el-form>
    </el-dialog>
    <el-drawer v-model="showDetail" title="事件证据与评级历史" size="min(760px, 100vw)">
      <template v-if="detail">
        <h2 class="text-lg">
          {{ detail.event.title }}
        </h2><div v-for="item in detail.history" :key="item.id" class="mb-5 border-b border-slate-200 pb-4">
          <a :href="item.input.url" target="_blank" rel="noopener noreferrer">{{ item.input.title || '原始新闻' }}</a><p class="text-xs text-slate-500">
            {{ item.input.source }} · 发布 {{ formatTime(item.input.published_at) }} · 进展 {{ formatTime(item.input.progress_at) }} · 分析 {{ formatTime(item.analyzed_at) }} · {{ item.model }} {{ item.historical ? '· 补分析' : '' }}
          </p>
          <template v-if="item.result">
            <p class="text-amber-600">
              {{ starText(item.result.stars) }} · {{ labels[item.result.verification] }}
            </p><p>{{ item.result.summary }}</p><p>依据：{{ item.result.reasoning }}</p><blockquote class="text-sm text-slate-600">
              {{ item.result.evidence }}
            </blockquote><p>不确定性：{{ item.result.uncertainty }}</p>
          </template>
        </div>
      </template>
    </el-drawer>
    <el-drawer v-model="showFailures" title="失败任务（最近100条）" size="min(700px, 100vw)">
      <div v-for="item in failures" :key="item.id" class="mb-4">
        <b>{{ item.input.title }}</b><p>{{ item.error }}</p><el-button @click="retry(item.id)">
          重试
        </el-button>
      </div>
    </el-drawer>
  </div>
</template>
