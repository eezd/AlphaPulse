<script setup lang="ts">
import type * as Risk from "@@/apis/risk/type"
import { formatTime } from "@@/utils/text"
import { listRiskQueueApi } from "@/common/apis/risk"
import { useUserStore } from "@/pinia/stores/user"

const emit = defineEmits<{ package: [id: number], event: [id: number] }>()
const user = useUserStore()
const result = ref<Risk.Queue | null>(null)
const expanded = ref(false)
const state = ref("running")
const stage = ref("")
const origin = ref("")
const jobId = ref<number | undefined>()
const page = ref(1)
const error = ref(false)
let version = 0
let busy = false
let alive = true
let timer: ReturnType<typeof setInterval> | undefined
const labels: Record<string, string> = { grouping: "新闻分包", analysis: "包研判", events: "事件时间线", outlook: "阅读编排", backfill: "历史新闻", running: "运行中", queued: "排队", waiting: "等待条件", failed: "失败", done: "已完成", unknown: "状态未知" }
async function load() {
  if (busy) return
  busy = true
  const seq = ++version
  const session = user.sessionVersion
  const query = { state: state.value || undefined, stage: stage.value || undefined, origin: origin.value || undefined, job_id: jobId.value || undefined, page: page.value }
  const sameQuery = () => query.state === (state.value || undefined) && query.stage === (stage.value || undefined) && query.origin === (origin.value || undefined) && query.job_id === (jobId.value || undefined) && query.page === page.value
  try {
    const data = await listRiskQueueApi(query)
    if (!sameQuery()) return
    if (alive && seq === version && session === user.sessionVersion) {
      result.value = data
      error.value = false
    }
  } catch {
    if (alive && seq === version && session === user.sessionVersion && sameQuery()) error.value = true
  } finally {
    if (seq === version) busy = false
  }
}
watch([state, stage, origin, jobId], () => {
  page.value = 1
  void load()
})
watch(page, () => void load())
watch(() => user.sessionVersion, () => {
  version++
  busy = false
  result.value = null
})
function visible() {
  if (!document.hidden) void load()
}
onMounted(() => {
  void load()
  timer = setInterval(visible, 3000)
  document.addEventListener("visibilitychange", visible)
})
onBeforeUnmount(() => {
  alive = false
  version++
  clearInterval(timer)
  document.removeEventListener("visibilitychange", visible)
})
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <b>AI 任务队列</b><el-button class="ml-3" link @click="expanded = !expanded">
        {{ expanded ? '收起详情' : '展开完整队列' }}
      </el-button>
    </template>
    <el-alert v-if="error" title="队列读取失败，保留上次状态" type="warning" :closable="false" />
    <el-alert v-if="result && !result.worker_alive" title="Worker 心跳已过期，运行状态未知；保留数据库任务进度" type="warning" :closable="false" />
    <p v-if="result">
      {{ result.enabled ? 'AI 已启用' : 'AI 已暂停' }} · 并发上限 {{ result.limit }} · 当前执行 {{ result.concurrency?.running ?? '未知' }} 项
    </p>
    <div class="flex flex-wrap gap-2">
      <el-select v-model="state" class="!w-32">
        <el-option v-for="s in ['running', 'queued', 'waiting', 'failed', 'done']" :key="s" :label="labels[s]" :value="s" />
      </el-select>
      <el-select v-if="expanded" v-model="stage" clearable placeholder="全部阶段" class="!w-36">
        <el-option v-for="s in ['grouping', 'analysis', 'events', 'outlook', 'backfill']" :key="s" :label="labels[s]" :value="s" />
      </el-select>
      <el-select v-if="expanded" v-model="origin" clearable placeholder="全部来源" class="!w-36">
        <el-option label="实时" value="realtime" /><el-option label="回补" value="history" />
      </el-select>
      <el-input-number v-if="expanded" v-model="jobId" :min="1" :precision="0" placeholder="批次编号" controls-position="right" />
      <el-button @click="load">
        刷新
      </el-button>
    </div>
    <p v-if="result">
      {{ labels[state] }} {{ result.total }} 项 · 更新 {{ formatTime(result.as_of) }}
    </p>
    <div v-for="task in result?.items.slice(0, expanded ? 20 : 5)" :key="task.key" class="my-3 break-words border-b pb-2">
      <b>{{ labels[task.stage] }} · {{ task.title }}</b>
      <a v-if="task.url && /^https?:\/\//i.test(task.url)" :href="task.url" target="_blank" rel="noopener noreferrer" class="ml-2 text-blue-600">新闻原文</a>
      <el-button v-if="task.stage === 'analysis'" link @click="emit('package', task.object_id)">
        查看包
      </el-button>
      <el-button v-if="task.stage === 'events'" link @click="emit('event', task.object_id)">
        查看事件
      </el-button>
      <p>{{ labels[task.state] }} · {{ task.phase }} {{ task.round ? `第${task.round}轮` : '' }} {{ task.tool ?? '' }} · {{ task.model ?? '待选定模型' }} {{ task.reasoning_effort ?? '' }}</p>
      <p v-if="task.updated_at">
        最近步骤 {{ formatTime(task.updated_at) }}
      </p>
      <p>开始 {{ formatTime(task.started_at) }} · {{ task.started_at ? `${Math.max(0, Math.floor((Date.parse(task.finished_at || result!.as_of) - Date.parse(task.started_at)) / 1000))}秒` : '等待领取' }} · {{ task.job_id ? `回补 #${task.job_id}` : '实时' }}</p>
      <p v-if="task.error" class="text-red-600">
        {{ task.error }}
      </p>
    </div>
    <el-empty v-if="result && !result.items.length" description="当前筛选下没有任务" :image-size="40" />
    <el-pagination v-if="expanded" v-model:current-page="page" :total="result?.total ?? 0" :page-size="20" layout="prev, pager, next" />
  </el-card>
</template>
