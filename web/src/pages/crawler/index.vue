<script lang="ts" setup>
import type { CrawlLog, SourcesInfo } from "@/apis/crawler"
import { Monitor, RefreshRight } from "@element-plus/icons-vue"
import { listLogs, listSources, restartCrawler } from "@/apis/crawler"
import { useEventSource } from "@/composables/useEventSource"
import SourcePanel from "@/pages/crawler/components/SourcePanel.vue"
import { formatTime } from "@/utils/text"

const MAX_LOGS = 1000
// 连续失败达到该次数时卡片告警
const FAILURE_ALERT = 3
const RESTART_TIMEOUT_MS = 60_000
const LEVELS = ["INFO", "WARNING", "ERROR"]

const info = ref<SourcesInfo>()
const logs = reactive<Record<string, CrawlLog[]>>({})
// 正在等待重新上线的来源
const restarting = reactive(new Set<string>())
// 日志面板筛选：空字符串为全部来源
const logSource = ref("")
const levels = ref([...LEVELS])
const autoScroll = ref(true)
const logBox = ref<HTMLElement>()

async function refresh() {
  info.value = await listSources()
}

const sourceNames = computed(() => Object.fromEntries(info.value?.sources.map(s => [s.code, s.name]) ?? []))

// 各来源日志按全局自增 id 合并
const visibleLogs = computed(() => Object.values(logs).flat().filter(log => (!logSource.value || log.source_code === logSource.value)
  && (levels.value.includes(log.level) || (log.level === "CRITICAL" && levels.value.includes("ERROR")))).sort((a, b) => a.id - b.id).slice(-MAX_LOGS))

watch(() => visibleLogs.value.at(-1)?.id, async () => {
  if (!autoScroll.value)
    return
  await nextTick()
  logBox.value?.scrollTo({ top: logBox.value.scrollHeight })
})

function levelClass(level: string) {
  if (level === "WARNING")
    return "bg-amber-500/20 text-amber-300"
  if (level === "ERROR" || level === "CRITICAL")
    return "bg-rose-500/20 text-rose-300"
  return "bg-sky-500/20 text-sky-300"
}

function appendLog(log: CrawlLog) {
  const list = (logs[log.source_code] ??= [])
  // SSE 可能早于历史日志到达，按 id 去重
  if (list.length && list.at(-1)!.id >= log.id)
    return
  list.push(log)
  if (list.length > MAX_LOGS)
    list.splice(0, list.length - MAX_LOGS)
  if (log.message.startsWith("子进程已启动"))
    restarting.delete(log.source_code)
}

const { connected } = useEventSource<CrawlLog>("/api/crawler/logs/stream", "log", appendLog)

async function loadHistory(code: string) {
  const history = await listLogs(code, 200)
  const live = logs[code] ?? []
  const firstLiveId = live[0]?.id ?? Infinity
  logs[code] = [...history.filter(log => log.id < firstLiveId), ...live]
}

async function restart(source: string) {
  const label = source === "all" ? "全部来源" : info.value?.sources.find(s => s.code === source)?.name
  await ElMessageBox.confirm(`确定重启 ${label} 的采集进程？`, "重启确认", { type: "warning" })
  const codes = source === "all" ? info.value!.sources.map(s => s.code) : [source]
  codes.forEach(code => restarting.add(code))
  try {
    await restartCrawler(source)
  } catch (error) {
    codes.forEach(code => restarting.delete(code))
    throw error
  }
  setTimeout(() => {
    const stuck = codes.filter(code => restarting.has(code))
    if (stuck.length) {
      stuck.forEach(code => restarting.delete(code))
      ElMessage.warning(`${stuck.join("、")} ${RESTART_TIMEOUT_MS / 1000}s 内未收到上线日志，请查看日志`)
    }
  }, RESTART_TIMEOUT_MS)
}

function formatBytes(bytes: number) {
  return bytes > 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

const timer = setInterval(refresh, 3000)
onBeforeUnmount(() => clearInterval(timer))

refresh().then(() => Promise.all(info.value!.sources.map(s => loadHistory(s.code))))
</script>

<template>
  <div v-if="info" class="flex flex-col gap-4">
    <el-card shadow="never" body-class="flex flex-wrap items-center gap-x-6 gap-y-2">
      <div class="flex items-center gap-2 text-sm text-slate-900 font-semibold">
        <el-icon><Monitor /></el-icon>
        全局状态
      </div>
      <div class="flex items-center gap-2 text-xs text-slate-500">
        采集管理进程
        <el-tag :type="info.manager_alive ? 'success' : 'danger'" size="small">
          ● {{ info.manager_alive ? "运行中" : "未运行" }}
        </el-tag>
      </div>
      <div class="flex items-center gap-2 text-xs text-slate-500">
        实时日志推送
        <el-tag :type="connected ? 'success' : 'danger'" size="small">
          ● {{ connected ? "已连接" : "断开" }}
        </el-tag>
      </div>
      <div class="flex items-center gap-2 text-xs text-slate-500">
        数据库占用
        <span class="num text-sm text-slate-900">{{ formatBytes(info.db_size_bytes) }}</span>
      </div>
      <div class="flex-1" />
      <el-button type="danger" plain :icon="RefreshRight" :loading="restarting.size > 0" @click="restart('all')">
        重启全部采集
      </el-button>
    </el-card>

    <div class="text-sm text-slate-900 font-semibold">
      采集来源（{{ info.sources.length }}）
    </div>
    <div class="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-4">
      <SourcePanel
        v-for="source in info.sources" :key="source.code"
        :source="source"
        :logs="logs[source.code] ?? []"
        :restarting="restarting.has(source.code)"
        :alert="source.consecutive_failures >= FAILURE_ALERT"
        @restart="restart(source.code)"
        @changed="refresh"
      />
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center gap-3">
          实时日志
          <div class="flex-1" />
          <el-select v-model="logSource" size="small" class="w-36" :empty-values="[null, undefined]">
            <el-option value="" label="全部来源" />
            <el-option v-for="s in info.sources" :key="s.code" :value="s.code" :label="s.name" />
          </el-select>
          <el-checkbox-group v-model="levels" size="small">
            <el-checkbox-button v-for="level in LEVELS" :key="level" :value="level">
              {{ level }}
            </el-checkbox-button>
          </el-checkbox-group>
          <el-switch v-model="autoScroll" active-text="自动滚动" size="small" />
        </div>
      </template>
      <div ref="logBox" class="h-100 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-200 font-mono leading-6">
        <div v-for="log in visibleLogs" :key="log.id" class="whitespace-pre-wrap break-all">
          <span class="text-slate-500">{{ formatTime(log.ts) }}</span>
          <span class="mx-2 rounded px-1" :class="levelClass(log.level)">{{ log.level }}</span>
          <span class="mr-2 text-teal-300">[{{ sourceNames[log.source_code] ?? log.source_code }}]</span>{{ log.message }}
        </div>
      </div>
    </el-card>
  </div>
</template>
