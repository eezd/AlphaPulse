<script lang="ts" setup>
import type { CrawlLog, SourceInfo } from "@/apis/crawler"
import type { NewsBrief } from "@/apis/news"
import dayjs from "dayjs"
import { patchSource } from "@/apis/crawler"
import { listNews } from "@/apis/news"
import { formatTime } from "@/utils/text"

const props = defineProps<{
  source: SourceInfo
  logs: CrawlLog[]
  restarting: boolean
}>()
const emit = defineEmits<{
  restart: []
  changed: []
}>()

const LEVELS = ["INFO", "WARNING", "ERROR"]
const levels = ref([...LEVELS])
const autoScroll = ref(true)
const logBox = ref<HTMLElement>()
const latest = ref<NewsBrief[]>([])
const interval = ref(props.source.interval_seconds)

const visibleLogs = computed(() => props.logs.filter(log => levels.value.includes(log.level) || (log.level === "CRITICAL" && levels.value.includes("ERROR"))))

watch(() => props.logs.length, async () => {
  if (!autoScroll.value)
    return
  await nextTick()
  logBox.value?.scrollTo({ top: logBox.value.scrollHeight })
}, { immediate: true })

async function loadLatest() {
  latest.value = (await listNews({ source: [props.source.code], limit: 15 })).items
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
  <div class="h-full flex flex-col gap-3">
    <el-descriptions :column="4" border size="small">
      <template #extra>
        <el-button type="warning" :loading="restarting" @click="emit('restart')">
          重启该来源
        </el-button>
      </template>
      <el-descriptions-item label="状态">
        <el-tag :type="statusTag.type" size="small">
          {{ statusTag.text }}
        </el-tag>
        <span v-if="source.process?.pid" class="ml-2 text-xs text-[var(--el-text-color-secondary)]">pid {{ source.process.pid }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="上次成功">
        {{ formatTime(source.last_success_at) }}
      </el-descriptions-item>
      <el-descriptions-item label="连续失败">
        <span :class="{ 'text-ap-down font-bold': source.consecutive_failures > 0 }">{{ source.consecutive_failures }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="下次运行">
        {{ formatTime(source.paused_until && source.paused_until > (source.process?.next_run_at ?? "") ? source.paused_until : source.process?.next_run_at) }}
      </el-descriptions-item>
      <el-descriptions-item label="今日新增 / 修订">
        {{ source.today_new }} / {{ source.today_revised }}
      </el-descriptions-item>
      <el-descriptions-item label="累计条目">
        {{ source.total_items }}
      </el-descriptions-item>
      <el-descriptions-item label="启用">
        <el-switch :model-value="source.enabled" @change="toggleEnabled" />
      </el-descriptions-item>
      <el-descriptions-item label="采集间隔">
        <el-input-number v-model="interval" :min="5" :max="86400" size="small" controls-position="right" class="w-28" @change="saveInterval" />
        秒
      </el-descriptions-item>
      <el-descriptions-item label="历史回补" :span="4">
        <span v-if="!source.supports_history" class="text-xs text-[var(--el-text-color-secondary)]">该来源的列表接口只能翻最近几页，无法回补历史</span>
        <div v-else class="flex items-center gap-3">
          <el-date-picker v-model="historyDate" type="date" value-format="YYYY-MM-DD" :clearable="false" size="small" class="!w-36" />
          <el-button size="small" type="primary" @click="startHistory">
            {{ source.history_position ? "继续回补" : "开始回补" }}
          </el-button>
          <el-button v-if="source.history_target" size="small" @click="cancelHistory">
            取消
          </el-button>
          <el-progress :percentage="history.percent" :status="history.status" class="w-60" />
          <span class="text-xs">{{ history.text }}</span>
        </div>
      </el-descriptions-item>
      <el-descriptions-item v-if="source.last_error" label="最近错误" :span="4">
        <span class="whitespace-pre-wrap text-xs text-ap-down">{{ source.last_error }}</span>
      </el-descriptions-item>
    </el-descriptions>

    <div class="min-h-0 flex flex-1 gap-3">
      <el-card class="w-2/5 flex flex-col" body-class="min-h-0 flex-1 overflow-auto" shadow="never">
        <template #header>
          <span>最新入库</span>
          <el-link :href="source.list_url" target="_blank" type="primary" class="ml-2">
            原站
          </el-link>
        </template>
        <div v-for="item in latest" :key="item.id" class="mb-2 text-sm">
          <span class="mr-2 text-xs text-[var(--el-text-color-secondary)]">{{ formatTime(item.published_at, "MM-DD HH:mm") }}</span>
          <el-link :href="item.url" target="_blank" class="font-sans">
            {{ item.title || item.excerpt.slice(0, 60) }}
          </el-link>
        </div>
      </el-card>

      <el-card class="w-3/5 flex flex-col" body-class="min-h-0 flex-1 flex flex-col" shadow="never">
        <template #header>
          <div class="flex items-center gap-3">
            <span>实时日志</span>
            <el-checkbox-group v-model="levels" size="small">
              <el-checkbox-button v-for="level in LEVELS" :key="level" :value="level">
                {{ level }}
              </el-checkbox-button>
            </el-checkbox-group>
            <el-switch v-model="autoScroll" active-text="自动滚动" size="small" />
          </div>
        </template>
        <div ref="logBox" class="min-h-0 flex-1 overflow-auto border border-ap-line border-solid bg-ap-sunken p-2 text-xs text-ap-text font-mono leading-5">
          <div v-for="log in visibleLogs" :key="log.id" class="whitespace-pre-wrap break-all">
            <span class="text-ap-faint">{{ formatTime(log.ts) }}</span>
            <span :class="{ 'text-ap-warn': log.level === 'WARNING', 'text-ap-down': log.level === 'ERROR' || log.level === 'CRITICAL' }" class="mx-2">{{ log.level }}</span>{{ log.message }}
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>
