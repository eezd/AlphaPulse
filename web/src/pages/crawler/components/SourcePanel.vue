<script lang="ts" setup>
import type { CrawlLog, SourceInfo } from "@/apis/crawler"
import type { NewsBrief } from "@/apis/news"
import { CircleCheck, RefreshRight } from "@element-plus/icons-vue"
import dayjs from "dayjs"
import { patchSource } from "@/apis/crawler"
import { listNews } from "@/apis/news"
import { formatTime } from "@/utils/text"

/** logs 只用来发现「本轮完成」以刷新最新入库；alert 为连续失败达到告警阈值 */
const props = defineProps<{
  source: SourceInfo
  logs: CrawlLog[]
  restarting: boolean
  alert: boolean
}>()
const emit = defineEmits<{
  restart: []
  changed: []
}>()

const latest = ref<NewsBrief[]>([])
const interval = ref(props.source.interval_seconds)

async function loadLatest() {
  latest.value = (await listNews({ source: [props.source.code], limit: 5 })).items
}

// 本来源有新的“本轮完成”日志时刷新最新入库列表
watch(() => props.logs.at(-1), (log) => {
  if (log?.message.startsWith("本轮完成") && !log.message.includes("新增 0，修订 0"))
    loadLatest()
})

async function toggleEnabled(enabled: boolean | string | number) {
  await patchSource(props.source.code, { enabled: Boolean(enabled) })
  emit("changed")
}

async function saveInterval() {
  if (interval.value === props.source.interval_seconds)
    return
  await patchSource(props.source.code, { interval_seconds: interval.value })
  ElMessage.success("已保存，下一轮起生效")
  emit("changed")
}

// 默认回补到 2026 年初；已设置过目标时沿用
const DEFAULT_HISTORY_TARGET = "2026-01-01"
const historyDate = ref(props.source.history_target ? dayjs(props.source.history_target).format("YYYY-MM-DD") : DEFAULT_HISTORY_TARGET)

const history = computed(() => {
  const { history_target: target, history_position: position, history_done: done } = props.source
  if (!target)
    return { text: position ? `未在回补，已回补到 ${formatTime(position, "YYYY-MM-DD HH:mm")}` : "未开始", percent: 0, status: undefined }
  const now = Date.now()
  const span = now - new Date(target).getTime()
  const covered = position ? now - new Date(position).getTime() : 0
  const percent = Math.min(100, Math.max(0, Math.round(covered / span * 1000) / 10))
  const reached = formatTime(position, "YYYY-MM-DD HH:mm")
  if (done && position && new Date(position) <= new Date(target))
    return { text: `已完成，已到 ${reached}`, percent: 100, status: "success" } as const
  if (done)
    return { text: `来源最早只能翻到 ${reached}，更早的数据接口不提供`, percent, status: "warning" } as const
  return { text: position ? `进行中，已到 ${reached}` : "等待下一轮开始", percent, status: undefined }
})

async function startHistory() {
  // 按北京时间当天 0 点
  await patchSource(props.source.code, { history_target: dayjs(`${historyDate.value}T00:00:00+08:00`).toISOString() })
  ElMessage.success("已开始，下一轮起分段回补")
  emit("changed")
}

async function cancelHistory() {
  await patchSource(props.source.code, { history_target: null })
  ElMessage.success("已取消，断点保留")
  emit("changed")
}

const statusTag = computed(() => {
  const process = props.source.process
  if (props.restarting || process?.state === "restarting")
    return { type: "warning", text: "重启中" } as const
  if (!process)
    return { type: "danger", text: "管理进程未运行" } as const
  if (process.state === "stopped")
    return { type: "danger", text: "子进程未运行" } as const
  if (!props.source.enabled)
    return { type: "info", text: "已停用" } as const
  if (props.source.paused_until && new Date(props.source.paused_until) > new Date())
    return { type: "warning", text: "失败退避中" } as const
  return { type: "success", text: "运行中" } as const
})

loadLatest()
</script>

<template>
  <div
    class="flex flex-col gap-3 border rounded-lg border-solid p-4 shadow-sm"
    :class="alert ? 'border-rose-300 bg-rose-50/50' : 'border-slate-200 bg-white'"
  >
    <div class="flex items-center gap-2">
      <span class="text-base text-slate-900 font-bold">{{ source.name }}</span>
      <el-tag v-if="alert" type="danger" effect="dark" size="small">
        告警
      </el-tag>
      <div class="flex-1" />
      <el-switch :model-value="source.enabled" @change="toggleEnabled" />
    </div>
    <div class="flex items-center gap-2 text-xs">
      <span class="h-2 w-2 rounded-full" :style="{ background: `var(--el-color-${statusTag.type})` }" />
      <span class="font-semibold" :style="{ color: `var(--el-color-${statusTag.type})` }">{{ statusTag.text }}</span>
      <span v-if="source.process?.pid" class="num text-slate-500">PID {{ source.process.pid }}</span>
    </div>

    <div class="grid grid-cols-2 gap-x-4 gap-y-2 border border-slate-100 rounded-lg border-solid bg-slate-50 p-3 text-xs text-slate-500">
      <div>
        上次成功
        <div class="num text-sm text-slate-900">
          {{ formatTime(source.last_success_at) }}
        </div>
      </div>
      <div>
        连续失败
        <div class="num text-sm" :class="source.consecutive_failures > 0 ? 'text-rose-600 font-bold' : 'text-slate-900'">
          {{ source.consecutive_failures }} 次
        </div>
      </div>
      <div>
        下次运行
        <div class="num text-sm text-[var(--el-color-primary)]">
          {{ formatTime(source.paused_until && source.paused_until > (source.process?.next_run_at ?? "") ? source.paused_until : source.process?.next_run_at) }}
        </div>
      </div>
      <div>
        累计条目
        <div class="num text-sm text-slate-900">
          {{ source.total_items.toLocaleString() }}
        </div>
      </div>
    </div>
    <div class="flex justify-between text-xs text-slate-500">
      <span>今日新增 <b class="num text-[var(--el-color-primary)]">+{{ source.today_new }}</b></span>
      <span>今日修订 <b class="num text-slate-900">{{ source.today_revised }}</b></span>
    </div>
    <div class="flex items-center justify-between border-t border-t-slate-100 border-t-solid pt-3 text-xs text-slate-500">
      采集间隔
      <span class="flex items-center gap-1">
        <el-input-number v-model="interval" :min="5" :max="86400" size="small" controls-position="right" class="w-28" @change="saveInterval" />
        秒
      </span>
    </div>

    <div class="border border-slate-200 rounded-lg border-solid bg-slate-50/60 p-3 text-xs text-slate-500">
      <div class="mb-2 flex items-center justify-between">
        历史回补
        <span v-if="source.supports_history" class="num text-[var(--el-color-primary)]">{{ history.percent }}%</span>
        <span v-else>不可用</span>
      </div>
      <template v-if="!source.supports_history">
        该来源的列表接口只能翻最近几页，无法回补历史
      </template>
      <template v-else>
        <div class="flex items-center gap-2">
          <el-date-picker v-model="historyDate" type="date" value-format="YYYY-MM-DD" :clearable="false" size="small" class="w-32!" />
          <el-button size="small" type="primary" plain class="flex-1" @click="startHistory">
            {{ source.history_position ? "继续回补" : "开始回补" }}
          </el-button>
          <el-button v-if="source.history_target" size="small" class="ml-0!" @click="cancelHistory">
            取消
          </el-button>
        </div>
        <el-progress :percentage="history.percent" :status="history.status" :show-text="false" :stroke-width="4" class="mt-2" />
        <div class="mt-1">
          {{ history.text }}
        </div>
      </template>
    </div>

    <div v-if="source.last_error" class="border border-rose-200 rounded-lg border-solid bg-rose-50 p-2 text-xs">
      <div class="mb-1 text-rose-700 font-semibold">
        最近错误
      </div>
      <div class="num max-h-24 overflow-auto whitespace-pre-wrap break-all text-rose-800">
        {{ source.last_error }}
      </div>
    </div>
    <div v-else class="flex items-center gap-1 text-xs text-slate-500">
      <el-icon class="text-emerald-600">
        <CircleCheck />
      </el-icon>
      最近错误：无
    </div>

    <div class="border border-slate-200 rounded-lg border-solid p-2 text-xs">
      <div class="mb-1 flex items-center justify-between text-slate-500">
        最新入库
        <el-link :href="source.list_url" target="_blank" type="primary" class="text-xs!">
          原站链接
        </el-link>
      </div>
      <div v-for="item in latest" :key="item.id" class="truncate leading-6">
        <span class="num mr-2 text-slate-400">{{ formatTime(item.published_at, "MM-DD HH:mm") }}</span>
        <a :href="item.url" target="_blank" class="text-slate-700 no-underline hover:text-[var(--el-color-primary)]">{{ item.title || item.excerpt.slice(0, 60) }}</a>
      </div>
      <div v-if="!latest.length" class="text-slate-400">
        暂无
      </div>
    </div>

    <el-button :type="alert ? 'danger' : 'default'" :icon="RefreshRight" :loading="restarting" class="mt-auto w-full" @click="emit('restart')">
      重启该来源
    </el-button>
  </div>
</template>
