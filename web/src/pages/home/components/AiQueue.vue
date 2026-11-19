<script setup lang="ts">
import type { AiQueue, AiQueueItem, AiWaitTarget } from "@@/apis/briefings/type"
import { getAiQueueApi, resetAiWaitsApi } from "@@/apis/briefings"
import { useSessionScope } from "@@/composables/useSessionScope"
import { useUserStore } from "@/pinia/stores/user"

const emit = defineEmits<{ analysis: [id: number] }>()
const scope = useSessionScope()
const user = useUserStore()
const queue = ref<AiQueue | null>(null)
const loading = ref(false)
const failed = ref(false)
const busy = ref(false)
const now = ref(Date.now())
const monotonicNow = ref(performance.now())
let receivedAt = monotonicNow.value
const notice = ref("")
const requested = ref<string[]>([])
const stages = { analysis: "单篇新闻分析", report: "报告生成" }
let closed = false
let sequence = 0
let poll: ReturnType<typeof setTimeout> | undefined
let clock: ReturnType<typeof setInterval> | undefined
const valid = () => !closed && scope.isCurrent()
const visible = () => typeof document === "undefined" || !document.hidden
function clearTimers() {
  clearTimeout(poll)
  clearInterval(clock)
}
function stop() {
  closed = true
  sequence++
  clearTimers()
  queue.value = null
  notice.value = ""
  requested.value = []
  loading.value = false
  busy.value = false
}
watch(() => user.sessionVersion, stop)
function fresh() {
  if (!queue.value?.worker_heartbeat_at || queue.value.progress_status !== "available") return false
  const heartbeatAge = Date.parse(queue.value.checked_at) - Date.parse(queue.value.worker_heartbeat_at)
  return heartbeatAge + monotonicNow.value - receivedAt <= 5000
}
function targets(): AiWaitTarget[] {
  if (failed.value || !fresh()) return []
  return (queue.value?.items ?? []).filter(item => item.can_retry_now && item.task_id && item.progress?.wait_id).map(item => ({ task_id: item.task_id!, wait_id: item.progress!.wait_id! }))
}
function key(target: AiWaitTarget) {
  return `${target.task_id}:${target.wait_id}`
}
function targetFor(item: AiQueueItem) {
  return targets().find(target => target.task_id === item.task_id && target.wait_id === item.progress?.wait_id)
}
function retryItem(item: AiQueueItem) {
  const target = targetFor(item)
  if (target) void retry(target)
}
function seconds(value: string, countdown = false) {
  const delta = countdown ? Date.parse(value) - now.value : now.value - Date.parse(value)
  return `${Math.max(0, Math.ceil(delta / 1000))} 秒`
}
function date(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—"
}
async function refresh() {
  if (!valid() || !visible()) return
  clearTimeout(poll)
  const seq = ++sequence
  loading.value = true
  try {
    const value = await getAiQueueApi()
    if (!valid() || seq !== sequence || !visible()) return
    queue.value = value
    receivedAt = performance.now()
    monotonicNow.value = receivedAt
    now.value = Date.now()
    failed.value = false
    const live = new Set(targets().map(key))
    requested.value = requested.value.filter(id => live.has(id))
  } catch {
    if (valid() && seq === sequence) failed.value = true
  } finally {
    if (valid() && seq === sequence) {
      loading.value = false
      if (visible()) poll = setTimeout(refresh, 5000)
    }
  }
}
async function retry(selected?: AiWaitTarget) {
  if (!valid() || busy.value) return
  // Keep the exact waits shown before confirmation, even if polling replaces the rows.
  const captured = selected ? [{ ...selected }] : targets().map(target => ({ ...target }))
  if (!captured.length) return
  busy.value = true
  try {
    if (!selected) {
      await ElMessageBox.confirm(`立即重试这 ${captured.length} 个等待？保留上下文，仅跳过本次等待。`, "全部等待立即重试", { type: "warning" })
    }
    if (!valid()) return
    let result
    try {
      result = await resetAiWaitsApi(captured)
    } catch {
      if (valid()) notice.value = "立即重试请求结果未确认，请刷新队列后查看"
      return
    } finally {
      if (valid()) await refresh()
    }
    if (!valid()) return
    requested.value = [...new Set([...requested.value, ...result.scheduled.map(key), ...result.already_requested.map(key)])]
    notice.value = `已请求立即重试 ${result.scheduled.length} 个；已请求过 ${result.already_requested.length} 个；已结束或失效 ${result.skipped.length} 个。`
  } catch {
    // User dismissed the batch confirmation.
  } finally {
    if (valid()) busy.value = false
  }
}
function visibilityChanged() {
  clearTimers()
  sequence++
  loading.value = false
  if (valid() && visible()) {
    now.value = Date.now()
    monotonicNow.value = performance.now()
    clock = setInterval(() => {
      now.value = Date.now()
      monotonicNow.value = performance.now()
    }, 1000)
    void refresh()
  }
}
onMounted(() => {
  document.addEventListener("visibilitychange", visibilityChanged)
  visibilityChanged()
})
onBeforeUnmount(() => {
  stop()
  document.removeEventListener("visibilitychange", visibilityChanged)
})
</script>

<template>
  <section class="ai-queue" aria-label="当前 AI 队列">
    <div class="queue-toolbar">
      <div>
        <strong>当前执行 {{ queue?.items.length ?? "—" }} 项</strong>
        <p class="muted">
          单篇新闻分析与报告生成；立即重试保留上下文与累计次数。
        </p>
      </div>
      <div class="queue-actions">
        <el-button :loading="loading" @click="refresh">
          刷新
        </el-button>
        <el-button type="warning" :disabled="busy || !targets().length" :loading="busy" @click="retry()">
          全部等待立即重试
        </el-button>
      </div>
    </div>
    <el-alert v-if="failed" type="error" :closable="false" title="队列读取失败，请刷新后重试；以下为上次读取的任务" />
    <el-alert v-if="queue && (!fresh() || queue.progress_status !== 'available')" type="warning" :closable="false" title="实时进度暂不可用，仍显示数据库中的执行任务，立即重试已禁用" />
    <el-alert v-if="notice" :title="notice" type="info" :closable="false" />
    <el-card v-for="item in queue?.items ?? []" :key="`${item.claim.kind}:${item.claim.id}`" shadow="never" class="queue-card">
      <div class="queue-toolbar">
        <div class="queue-title">
          <el-tag>{{ stages[item.stage] }}</el-tag>
          <router-link v-if="item.report" :to="{ path: '/reports', query: { report: item.report.id } }">
            {{ item.report.report_date }} · 报告 #{{ item.report.id }} · v{{ item.report.version_no }}
          </router-link>
          <el-button v-else-if="item.analysis_id" link type="primary" @click="emit('analysis', item.analysis_id)">
            {{ item.news[0]?.title ?? '原文不可用' }}
          </el-button>
          <span v-else>原文不可用</span>
        </div>
        <el-button v-if="targetFor(item)" type="warning" size="small" :disabled="busy || requested.includes(key(targetFor(item)!))" @click="retryItem(item)">
          {{ requested.includes(key(targetFor(item)!)) ? '已请求立即重试' : '立即重试' }}
        </el-button>
      </div>
      <template v-if="item.progress && fresh() && !failed">
        <p><strong>{{ item.progress.phase }}</strong> · {{ item.progress.operation ?? stages[item.stage] }}<span v-if="item.progress.tool"> · {{ item.progress.tool }}</span></p>
        <p v-if="item.progress.current_news_id">
          当前新闻：{{ item.news.find(news => news.id === item.progress?.current_news_id)?.title ?? `新闻 #${item.progress.current_news_id}` }}
        </p>
        <p class="queue-meta">
          模型 {{ item.progress.model ?? '准备中' }} · 思考强度 {{ item.progress.reasoning_effort ?? '默认' }}<span v-if="item.progress.round"> · 第 {{ item.progress.round }} 轮</span> · 累计重试 {{ item.progress.retry_attempt ?? 0 }} 次
        </p>
        <p v-if="item.progress.retry_at">
          下次重试 {{ date(item.progress.retry_at) }}（{{ seconds(item.progress.retry_at, true) }}后）
        </p>
        <p class="muted">
          最近进度 {{ date(item.progress.updated_at) }}
        </p>
      </template>
      <p v-else class="muted">
        实时进度暂不可用
      </p>
      <p class="muted">
        开始 {{ date(item.started_at) }} · 已持续 {{ seconds(item.started_at) }} · 任务 #{{ item.claim.id }}
      </p>
      <details v-if="item.news.length" open>
        <summary>关联新闻 {{ item.news.length }} 篇</summary>
        <div v-for="news in item.news" :key="news.id" class="queue-news">
          <span>{{ news.title }}</span>
          <span class="muted">{{ news.source_code }} · {{ date(news.published_at) }}</span>
          <a v-if="news.url" :href="news.url" target="_blank" rel="noopener noreferrer">查看原文</a>
        </div>
      </details>
    </el-card>
    <el-empty v-if="queue && !queue.items.length && !failed" description="当前没有执行中的 AI 任务，待处理数量见上方队列统计" />
  </section>
</template>

<style scoped>
.ai-queue {
  display: grid;
  gap: 12px;
}
.queue-toolbar,
.queue-actions,
.queue-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.queue-toolbar {
  justify-content: space-between;
}
.queue-title {
  min-width: 0;
  flex: 1;
}
.queue-title :deep(.el-button),
.queue-news :deep(.el-button) {
  white-space: normal;
  text-align: left;
  height: auto;
  line-height: 1.6;
}
.queue-title :deep(.el-button > span),
.queue-news :deep(.el-button > span) {
  overflow-wrap: anywhere;
}
.queue-news {
  display: grid;
  gap: 4px;
  margin-top: 12px;
  padding-left: 12px;
  border-left: 2px solid var(--el-border-color);
}
.queue-meta,
.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
summary {
  cursor: pointer;
}
@media (max-width: 600px) {
  .queue-toolbar {
    align-items: flex-start;
  }
  .queue-title {
    flex-basis: 100%;
  }
}
</style>
