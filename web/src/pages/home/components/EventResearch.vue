<script setup lang="ts">
import type * as Risk from "@@/apis/risk/type"
import { formatTime } from "@@/utils/text"
import * as api from "@/common/apis/risk"
import { useUserStore } from "@/pinia/stores/user"

const emit = defineEmits<{ package: [id: number] }>()
const user = useUserStore()
const items = ref<Risk.NewsEvent[]>([])
const total = ref(0)
const page = ref(1)
const detail = ref<Risk.EventDetail | null>(null)
const opened = ref(false)
const detailError = ref(false)
const detailId = ref(0)
const range = ref<[Date, Date] | null>(null)
const modes = ref<("missing" | "review")[]>(["missing"])
const preview = ref<Risk.ReprocessPreview | null>(null)
const jobs = ref<Risk.ReprocessJob[]>([])
const batch = ref<Awaited<ReturnType<typeof api.getReprocessDetailApi>> | null>(null)
const batchId = ref(0)
const batchPage = ref(1)
const batchLoading = ref(false)
const batchError = ref(false)
let batchSequence = 0
async function openBatch(id: number, page = 1) {
  if (batchId.value !== id) batch.value = null
  batchId.value = id
  batchPage.value = page
  batchLoading.value = true
  const version = user.sessionVersion
  const seq = ++batchSequence
  try {
    const data = await api.getReprocessDetailApi(id, page)
    if (!active || seq !== batchSequence || version !== user.sessionVersion) return
    batch.value = data
    batchError.value = false
  } catch {
    if (active && seq === batchSequence && version === user.sessionVersion) batchError.value = true
  } finally {
    if (active && seq === batchSequence && version === user.sessionVersion) batchLoading.value = false
  }
}
const busy = ref(false)
const error = ref(false)
let sequence = 0
let detailSequence = 0
let operation = 0
let active = true
async function load() {
  const seq = ++sequence
  const version = user.sessionVersion
  try {
    const [events, runs] = await Promise.all([api.listNewsEventsApi(page.value), api.listReprocessApi()])
    if (!active || seq !== sequence || version !== user.sessionVersion) return
    items.value = events.items
    total.value = events.total
    jobs.value = runs
    error.value = false
    if (batchId.value && !batchLoading.value) await openBatch(batchId.value, batchPage.value)
  } catch {
    if (active && seq === sequence && version === user.sessionVersion) error.value = true
  }
}
async function open(id: number) {
  const seq = ++detailSequence
  const version = user.sessionVersion
  detail.value = null
  detailError.value = false
  detailId.value = id
  opened.value = true
  try {
    const result = await api.getNewsEventApi(id)
    if (active && opened.value && seq === detailSequence && version === user.sessionVersion) detail.value = result
  } catch {
    if (active && seq === detailSequence && version === user.sessionVersion) detailError.value = true
  }
}
async function inspect() {
  if (!range.value || busy.value) return
  const version = user.sessionVersion
  const data = { start: range.value[0].toISOString(), end: range.value[1].toISOString(), modes: [...modes.value] }
  const op = ++operation
  busy.value = true
  try {
    const result = await api.previewReprocessApi(data)
    if (active && version === user.sessionVersion && range.value && data.start === range.value[0].toISOString() && data.end === range.value[1].toISOString() && data.modes.join() === modes.value.join()) preview.value = result
  } finally {
    if (active && op === operation && version === user.sessionVersion) busy.value = false
  }
}
async function start() {
  if (!range.value || !preview.value || busy.value) return
  const version = user.sessionVersion
  const data = { start: range.value[0].toISOString(), end: range.value[1].toISOString(), modes: [...modes.value] }
  const previewId = preview.value.preview_id
  await ElMessageBox.confirm(`回补 ${preview.value.articles} 篇新闻将产生模型费用，关联包和事件也会更新。是否启动？`, "AI 历史回补")
  if (!active || version !== user.sessionVersion || busy.value) return
  const op = ++operation
  busy.value = true
  try {
    if (!preview.value || preview.value.preview_id !== previewId || data.modes.join() !== modes.value.join() || !range.value || data.start !== range.value[0].toISOString() || data.end !== range.value[1].toISOString()) return
    await api.startReprocessApi({ preview_id: previewId })
    if (active && version === user.sessionVersion) {
      preview.value = null
      await load()
    }
  } finally {
    if (active && op === operation && version === user.sessionVersion) busy.value = false
  }
}
async function action(id: number, retry: boolean) {
  const version = user.sessionVersion
  await (retry ? api.retryReprocessApi(id) : api.cancelReprocessApi(id))
  if (active && version === user.sessionVersion) await load()
}
watch([range, modes], () => {
  preview.value = null
})
watch(page, load)
watch(() => user.sessionVersion, () => {
  batchSequence++
  sequence++
  detailSequence++
  operation++
  busy.value = false
  detailError.value = false
  items.value = []
  jobs.value = []
  batch.value = null
  batchId.value = 0
  batchLoading.value = false
  batchError.value = false
  detail.value = null
  preview.value = null
  opened.value = false
})
watch(opened, (value) => {
  if (!value) detailSequence++
})
let timer: ReturnType<typeof setInterval>
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (!document.hidden) void load()
  }, 15000)
})
onBeforeUnmount(() => {
  active = false
  batchSequence++
  sequence++
  detailSequence++
  clearInterval(timer)
})
defineExpose({ open })
</script>

<template>
  <el-card shadow="never">
    <template #header>
      事件与时间线
    </template>
    <el-alert v-if="error" title="事件读取失败，可重试" type="warning" :closable="false" />
    <el-button link @click="load">
      刷新
    </el-button>
    <div v-for="event in items" :key="event.id" class="my-3">
      <el-button link @click="open(event.id)">
        {{ event.title }}
      </el-button>
      <p>{{ event.result?.summary || '正在整理组成包的时间线' }}</p>
      <p v-if="event.result">
        重要性 {{ event.result.importance_stars }} 星 · 风险 {{ event.result.risk_stars }}X
      </p>
      <p v-if="event.error">
        更新失败：{{ event.error }}；保留上一成功版本
      </p>
    </div>
    <el-pagination v-model:current-page="page" :total="total" :page-size="20" layout="prev, pager, next" />
    <details class="mt-4">
      <summary>AI 历史回补</summary>
      <p>按新闻发布时间选择范围，使用现存最新修订。不会还原历史时点；共享包和事件可能同时更新。AI 暂停时任务等待。</p>
      <el-checkbox-group v-model="modes">
        <el-checkbox value="missing">
          补齐未完成研判
        </el-checkbox>
        <el-checkbox value="review">
          已研判重新核对（重新分包及核验）
        </el-checkbox>
      </el-checkbox-group>
      <el-date-picker v-model="range" type="datetimerange" class="!max-w-full" start-placeholder="起始时间" end-placeholder="结束时间" />
      <el-button :loading="busy" :disabled="!range || !modes.length" @click="inspect">
        预览范围
      </el-button>
      <template v-if="preview">
        <p>范围共 {{ preview.total }} 篇 · 未完成 {{ preview.missing }} · 已研判 {{ preview.review }} · 本次 {{ preview.articles }} 篇新闻 · {{ preview.packages }} 个包 · {{ preview.events }} 个事件</p>
        <p v-for="(model, stage) in preview.models" :key="stage">
          {{ ({ grouping: '分包', analysis: '研判', outlook: '阅读编排' })[stage] }}：{{ model.model }} · 思考强度 {{ model.reasoning_effort ?? '模型默认' }}
        </p>
        <el-button :loading="busy" :disabled="!preview.articles" @click="start">
          启动回补
        </el-button>
      </template>
      <div v-for="job in jobs" :key="job.id" class="my-2">
        #{{ job.id }} · {{ job.state }} · 新闻处理 {{ job.completed }}/{{ job.total }}
        <p v-if="job.request">
          {{ formatTime(job.request.start) }} 至 {{ formatTime(job.request.end) }} · {{ job.request.modes.map(m => m === 'missing' ? '补齐未完成' : '重新核对').join('、') }}
        </p>
        <p v-if="job.counts">
          成功 {{ job.counts.done ?? 0 }} · 处理中 {{ job.counts.processing ?? 0 }} · 待处理 {{ job.counts.pending ?? 0 }} · 失败 {{ job.counts.failed ?? 0 }} · 跳过 {{ job.counts.skipped ?? 0 }}
        </p>
        <p v-for="(counts, stage) in job.stages" :key="stage">
          {{ stage === 'analysis' ? '包研判' : '事件时间线' }}：待处理 {{ counts.pending ?? 0 }} · 运行 {{ counts.processing ?? 0 }} · 完成 {{ counts.done ?? 0 }} · 失败 {{ counts.failed ?? 0 }}
        </p>
        <el-button link @click="openBatch(job.id)">
          查看新闻明细
        </el-button>
        <el-button v-if="['pending', 'running'].includes(job.state)" link @click="action(job.id, false)">
          取消
        </el-button>
        <el-button v-if="job.state === 'failed'" link @click="action(job.id, true)">
          重试失败项
        </el-button>
        <p v-for="failure in job.failures" :key="`${failure.stage}:${failure.id}`">
          {{ ({ grouping: "新闻初筛", analysis: "包研判", events: "事件时间线", outlook: "阅读编排" })[failure.stage] }} #{{ failure.id }}：{{ failure.error }}
        </p>
      </div>
      <div v-if="batchId">
        <h4>回补 #{{ batchId }} 新闻明细</h4>
        <el-alert v-if="batchError" title="新闻明细读取失败，保留上次进度，可刷新重试" type="warning" :closable="false" />
        <el-button :loading="batchLoading" @click="openBatch(batchId, batchPage)">
          刷新明细
        </el-button>
        <p v-for="item in batch?.items" :key="item.id">
          {{ item.title }}（新闻 #{{ item.news_id }}）· {{ item.mode === 'missing' ? '补齐' : item.mode === 'review' ? '重新核对' : '旧批次' }} · {{ item.state }} {{ item.error }}
        </p>
        <el-pagination v-if="batch" :current-page="batchPage" :total="batch.total" :page-size="20" layout="prev, pager, next" @current-change="openBatch(batchId, $event)" />
      </div>
    </details>
  </el-card>
  <el-drawer v-model="opened" title="事件发展时间线" size="min(760px, 100vw)">
    <el-alert v-if="detailError" title="事件详情读取失败" type="warning" :closable="false" />
    <el-button v-if="detailError" @click="open(detailId)">
      重试
    </el-button>
    <template v-if="detail">
      <h2>{{ detail.title }}</h2><p>{{ detail.result?.summary }}</p>
      <el-alert v-if="detail.error" :title="detail.error" type="warning" :closable="false" />
      <div v-for="(point, index) in detail.result?.timeline_points" :key="index" class="my-4 border-l-2 pl-3">
        <b>{{ point.time_text || '发生时间未知' }}</b> · {{ point.time_precision }} · {{ point.verification }}
        <p>{{ point.description }}</p><p>报道时间：{{ formatTime(point.reported_at) }}</p>
        <el-button link @click="emit('package', point.package_id)">
          包 #{{ point.package_id }} · v{{ point.package_revision }}
        </el-button>
        <blockquote v-for="(ref, n) in point.evidence_refs" :key="n">
          {{ ref.quote }}
        </blockquote>
      </div>
      <p>反证：{{ detail.result?.counter_evidence }}</p><p>不确定性：{{ detail.result?.uncertainty }}</p><p>意见：{{ detail.result?.opinions }}</p><p>关注：{{ detail.result?.watchpoints }}</p>
      <p>组成包</p>
      <div v-for="item in detail.packages" :key="item.id">
        <el-button link @click="emit('package', item.id)">
          {{ item.title }}
        </el-button>
      </div>
      <p>版本历史</p>
      <details v-for="version in detail.versions" :key="version.id" class="my-2">
        <summary>v{{ version.revision }} · {{ version.state }} {{ version.error }}</summary>
        <p>{{ version.result?.summary }}</p>
        <p v-for="(point, index) in version.result?.timeline_points" :key="index">
          {{ point.time_text }} · {{ point.description }} · 包 #{{ point.package_id }} v{{ point.package_revision }}
        </p>
        <p>{{ version.result?.uncertainty }}</p>
      </details>
    </template>
  </el-drawer>
</template>
