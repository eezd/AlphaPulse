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
const preview = ref<Risk.ReprocessPreview | null>(null)
const jobs = ref<Risk.ReprocessJob[]>([])
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
  const data = { start: range.value[0].toISOString(), end: range.value[1].toISOString() }
  const op = ++operation
  busy.value = true
  try {
    const result = await api.previewReprocessApi(data)
    if (active && version === user.sessionVersion && range.value && data.start === range.value[0].toISOString() && data.end === range.value[1].toISOString()) preview.value = result
  } finally {
    if (active && op === operation && version === user.sessionVersion) busy.value = false
  }
}
async function start() {
  if (!range.value || !preview.value || busy.value) return
  const version = user.sessionVersion
  const data = { start: range.value[0].toISOString(), end: range.value[1].toISOString() }
  await ElMessageBox.confirm(`重整理 ${preview.value.articles} 篇新闻将产生模型费用，关联包和事件也会更新。是否启动？`, "重整理")
  if (!active || version !== user.sessionVersion || busy.value) return
  const op = ++operation
  busy.value = true
  try {
    await api.startReprocessApi(data)
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
watch(range, () => {
  preview.value = null
})
watch(page, load)
watch(() => user.sessionVersion, () => {
  sequence++
  detailSequence++
  operation++
  busy.value = false
  detailError.value = false
  items.value = []
  jobs.value = []
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
      <summary>按时间范围重整理新闻</summary>
      <p>按新闻发布时间选择范围，使用现存最新修订。不会还原历史时点；共享包和事件可能同时更新。AI 暂停时任务等待。</p>
      <el-date-picker v-model="range" type="datetimerange" class="!max-w-full" start-placeholder="起始时间" end-placeholder="结束时间" />
      <el-button :loading="busy" @click="inspect">
        预览范围
      </el-button>
      <template v-if="preview">
        <p>{{ preview.articles }} 篇新闻 · {{ preview.packages }} 个包 · {{ preview.events }} 个事件</p>
        <el-button :loading="busy" @click="start">
          启动重整理
        </el-button>
      </template>
      <div v-for="job in jobs" :key="job.id" class="my-2">
        #{{ job.id }} · {{ job.state }} · {{ job.completed }}/{{ job.total }}
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
