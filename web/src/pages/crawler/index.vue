<script lang="ts" setup>
import type { SourcesInfo } from "@@/apis/crawler/type"
import { createRssApi, listSourcesApi, restartCrawlerApi } from "@@/apis/crawler"
import { useSessionScope } from "@@/composables/useSessionScope"
import { formatTime } from "@@/utils/text"
import { Monitor, RefreshRight } from "@element-plus/icons-vue"
import { logRequestError } from "@/http/axios"
import CalendarSourcePanel from "./components/CalendarSourcePanel.vue"
import SourcePanel from "./components/SourcePanel.vue"
import { useCrawlerLogs } from "./composables/useCrawlerLogs"

const rssVisible = ref(false)
const rssSaving = ref(false)
const rssDraft = reactive({ name: "", url: "", interval_seconds: 300 })
async function addRss() {
  if (rssSaving.value) return
  rssSaving.value = true
  sessionScope.assertCurrent()
  try {
    await createRssApi({ ...rssDraft })
    if (!sessionScope.isCurrent()) return
    rssVisible.value = false
    await refresh()
  } finally {
    rssSaving.value = false
  }
}
const sessionScope = useSessionScope()

// 连续失败达到该次数时卡片告警
const FAILURE_ALERT = 3
const RESTART_TIMEOUT_MS = 60_000

const info = ref<SourcesInfo>()
const loading = ref(false)
const failed = ref(false)
const detailVisible = ref(false)
const detailCode = ref("")
const detailKind = ref<"news" | "calendar">("news")
const newsDetail = computed(() => detailKind.value === "news" ? info.value?.sources.find(s => s.code === detailCode.value) : undefined)
const calendarDetail = computed(() => detailKind.value === "calendar" ? info.value?.calendar_sources.find(s => s.code === detailCode.value) : undefined)
function openDetail(code: string, kind: "news" | "calendar") {
  detailCode.value = code
  detailKind.value = kind
  detailVisible.value = true
}
const collector = ref("all")
const region = ref("all")
const regions = { all: "全部地域", CN: "中国", US: "美国", EA: "欧元区", GB: "英国", DE: "德国", FR: "法国", GLOBAL: "全球" }
const newsSources = computed(() => info.value?.sources.filter(s => region.value === "all" || s.region_code === region.value) ?? [])
const calendarSources = computed(() => info.value?.calendar_sources.filter(s => region.value === "all" || s.region_code === region.value) ?? [])
// 正在等待重新上线的来源
const restarting = reactive(new Set<string>())
const { logs, logSource, levels, autoScroll, logBox, visibleLogs, levelClass, connected, loadHistory, LEVELS } = useCrawlerLogs(code => restarting.delete(code))
// 日志面板筛选：空字符串为全部来源

async function refresh() {
  loading.value = info.value === undefined
  try {
    info.value = await listSourcesApi()
    failed.value = false
  } catch (error) {
    failed.value = true
    throw error
  } finally {
    loading.value = false
  }
}

const sourceNames = computed(() => Object.fromEntries(info.value?.sources.map(s => [s.code, s.name]) ?? []))

async function restart(source: string) {
  const label = source === "all" ? "全部来源" : info.value?.sources.find(s => s.code === source)?.name
  await ElMessageBox.confirm(`确定重启 ${label} 的采集进程？`, "重启确认", { type: "warning" })
  sessionScope.assertCurrent()
  const codes = source === "all" ? info.value!.sources.map(s => s.code) : [source]
  codes.forEach(code => restarting.add(code))
  try {
    await restartCrawlerApi(source)
  } catch (error) {
    codes.forEach(code => restarting.delete(code))
    throw error
  }
  setTimeout(() => {
    if (!sessionScope.isCurrent()) return
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

const timer = setInterval(() => refresh().catch(logRequestError), 3000)
onBeforeUnmount(() => clearInterval(timer))

onMounted(async () => {
  await refresh()
  await Promise.all(info.value!.sources.map(s => loadHistory(s.code)))
})
</script>

<template>
  <el-dialog v-model="rssVisible" title="新增RSS/Atom订阅（保存后默认暂停）">
    <el-form label-position="top">
      <el-form-item label="名称">
        <el-input v-model="rssDraft.name" />
      </el-form-item><el-form-item label="公开订阅地址">
        <el-input v-model="rssDraft.url" />
      </el-form-item><el-form-item label="间隔（秒）">
        <el-input-number v-model="rssDraft.interval_seconds" :min="60" :max="86400" />
      </el-form-item><el-button :loading="rssSaving" @click="addRss">
        保存订阅
      </el-button>
    </el-form>
  </el-dialog>
  <el-button @click="rssVisible = true">
    新增 RSS/Atom 订阅
  </el-button>
  <div v-if="!info" v-loading="loading" class="min-h-40">
    <el-result v-if="failed" icon="error" title="采集状态读取失败">
      <template #extra>
        <el-button @click="refresh">
          重试
        </el-button>
      </template>
    </el-result>
  </div>
  <div v-else class="flex flex-col gap-4">
    <el-alert v-if="failed" type="error" :closable="false" title="状态刷新失败，以下为上次读取的数据" />
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
        重启全部新闻采集
      </el-button>
    </el-card>

    <div class="flex flex-wrap items-center gap-3">
      <el-select v-model="collector" class="w-40">
        <el-option value="all" label="全部采集类型" /><el-option value="news" label="新闻" /><el-option value="calendar" label="经济日历" />
      </el-select>
      <el-select v-model="region" class="w-36">
        <el-option v-for="(name, code) in regions" :key="code" :value="code" :label="name" />
      </el-select>
    </div>
    <el-card v-if="collector !== 'calendar'" shadow="never" body-class="p-0">
      <template #header>
        新闻来源 · {{ newsSources.length }}
      </template>
      <el-table :data="newsSources" size="small" @row-click="row => openDetail(row.code, 'news')">
        <el-table-column prop="name" label="来源" min-width="170" />
        <el-table-column label="地域" width="95">
          <template #default="{ row }">
            {{ regions[row.region_code as keyof typeof regions] ?? row.region_code }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled && row.process?.state === 'running' ? 'success' : 'info'">
              {{ !row.enabled ? '已停用' : row.process?.state === 'running' ? '运行中' : '未运行' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="频率" width="85">
          <template #default="{ row }">
            {{ row.interval_seconds }} 秒
          </template>
        </el-table-column>
        <el-table-column label="今日新增 / 修订" width="130">
          <template #default="{ row }">
            {{ row.today_new }} / {{ row.today_revised }}
          </template>
        </el-table-column>
        <el-table-column label="最近成功" width="155">
          <template #default="{ row }">
            {{ row.last_success_at ? formatTime(row.last_success_at) : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="异常 / 回补" min-width="230">
          <template #default="{ row }">
            <span :class="row.consecutive_failures >= FAILURE_ALERT ? 'text-red-600' : ''">{{ row.last_error || (row.history_done ? '历史回补完成' : row.history_position ? `回补至 ${row.history_position}` : '—') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDetail(row.code, 'news')">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
    <el-card v-if="collector !== 'news'" shadow="never" body-class="p-0">
      <template #header>
        经济日历 · {{ calendarSources.length }} <span class="text-xs text-slate-500 font-normal">（只读）</span>
      </template>
      <el-table :data="calendarSources" size="small" @row-click="row => openDetail(row.code, 'calendar')">
        <el-table-column prop="name" label="来源" min-width="170" />
        <el-table-column label="地域" width="95">
          <template #default="{ row }">
            {{ regions[row.region_code as keyof typeof regions] }}
          </template>
        </el-table-column>
        <el-table-column label="采集进程" width="110">
          <template #default="{ row }">
            <el-tag :type="row.worker_alive ? 'success' : 'danger'" size="small">
              {{ row.worker_alive ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="日程更新" min-width="155">
          <template #default="{ row }">
            {{ row.schedule_success_at ? formatTime(row.schedule_success_at) : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="下个事件" min-width="155">
          <template #default="{ row }">
            {{ row.next_event_at ? formatTime(row.next_event_at) : '—' }}
          </template>
        </el-table-column>
        <el-table-column prop="overdue_count" label="待取结果" width="90" />
        <el-table-column label="异常" min-width="230">
          <template #default="{ row }">
            <span class="text-red-600">{{ row.errors.join('；') || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDetail(row.code, 'calendar')">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
    <el-drawer v-model="detailVisible" :title="newsDetail?.name ?? calendarDetail?.name ?? '来源详情'" size="min(640px, 100vw)">
      <SourcePanel v-if="newsDetail" :key="newsDetail.code" :source="newsDetail" :logs="logs[newsDetail.code] ?? []" :restarting="restarting.has(newsDetail.code)" :alert="newsDetail.consecutive_failures >= FAILURE_ALERT" @restart="restart(newsDetail.code)" @changed="refresh" />
      <CalendarSourcePanel v-if="calendarDetail" :source="calendarDetail" />
    </el-drawer>

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
