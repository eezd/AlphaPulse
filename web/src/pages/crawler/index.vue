<script lang="ts" setup>
import type { CrawlLog, SourcesInfo } from "@/apis/crawler"
import { listLogs, listSources, restartCrawler } from "@/apis/crawler"
import { useEventSource } from "@/composables/useEventSource"
import SourcePanel from "@/pages/crawler/components/SourcePanel.vue"

const MAX_LOGS = 1000
// 连续失败达到该次数时 TAB 标红
const FAILURE_ALERT = 3
const RESTART_TIMEOUT_MS = 60_000

const info = ref<SourcesInfo>()
const activeTab = ref<string>()
const logs = reactive<Record<string, CrawlLog[]>>({})
// 正在等待重新上线的来源
const restarting = reactive(new Set<string>())

async function refresh() {
  info.value = await listSources()
  activeTab.value ??= info.value.sources[0]?.code
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
  <div v-if="info" class="h-full flex flex-col gap-3">
    <div class="flex items-center gap-3">
      <el-tag :type="info.manager_alive ? 'success' : 'danger'">
        {{ info.manager_alive ? "管理进程运行中" : "管理进程未运行" }}
      </el-tag>
      <el-tag :type="connected ? 'success' : 'danger'">
        {{ connected ? "日志推送已连接" : "日志推送断开" }}
      </el-tag>
      <span class="text-sm text-[var(--el-text-color-secondary)]">数据库 {{ formatBytes(info.db_size_bytes) }}</span>
      <div class="flex-1" />
      <el-button type="danger" :loading="restarting.size > 0" @click="restart('all')">
        重启全部采集
      </el-button>
    </div>
    <el-tabs v-model="activeTab" type="border-card" class="crawler-tabs min-h-0 flex flex-1 flex-col">
      <el-tab-pane v-for="source in info.sources" :key="source.code" :name="source.code" class="h-full">
        <template #label>
          <span :class="{ 'text-red-500 font-bold': source.consecutive_failures >= FAILURE_ALERT }">
            {{ source.name }}
          </span>
          <el-badge v-if="source.consecutive_failures >= FAILURE_ALERT" :value="source.consecutive_failures" class="ml-1" />
        </template>
        <SourcePanel
          :source="source"
          :logs="logs[source.code] ?? []"
          :restarting="restarting.has(source.code)"
          @restart="restart(source.code)"
          @changed="refresh"
        />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.crawler-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
}
</style>
