<script lang="ts" setup>
import type { SourceInfo, SourcesInfo } from "@@/apis/crawler/type"
import { createRssApi, listSourcesApi, restartCrawlerApi } from "@@/apis/crawler"
import { useSessionScope } from "@@/composables/useSessionScope"
import { downloadCsv } from "@@/utils/export"
import { formatTime } from "@@/utils/text"
import { Monitor, RefreshRight } from "@element-plus/icons-vue"
import { logRequestError } from "@/http/axios"
import CalendarSourcePanel from "./components/CalendarSourcePanel.vue"
import SourcePanel from "./components/SourcePanel.vue"
import { useCrawlerLogs } from "./composables/useCrawlerLogs"
import { sourceStatus } from "./sourceStatus"

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
const expandedLogs = ref(false)
const failed = ref(false)
const detailVisible = ref(false)
const wide = ref(false)
const sourceView = ref("cards")
let viewport: MediaQueryList
const updateViewport = () => wide.value = viewport.matches
onMounted(() => {
  viewport = window.matchMedia("(min-width: 1380px)")
  updateViewport()
  viewport.addEventListener("change", updateViewport)
})
onBeforeUnmount(() => viewport?.removeEventListener("change", updateViewport))
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
const searchText = ref("")
const statusFilter = ref("all")
function matchesSource(source: SourceInfo) {
  if (searchText.value && !`${source.name} ${source.code}`.toLowerCase().includes(searchText.value.toLowerCase())) return false
  const unhealthy = ["warning", "danger"].includes(sourceStatus(source, restarting.has(source.code)).type)
  return statusFilter.value === "all" || (statusFilter.value === "paused" ? !source.enabled : statusFilter.value === "error" ? source.enabled && unhealthy : source.enabled && !unhealthy)
}
const regions = { all: "全部地域", CN: "中国", US: "美国", EA: "欧元区", GB: "英国", DE: "德国", FR: "法国", GLOBAL: "全球" }
const newsSources = computed(() => info.value?.sources.filter(matchesSource).filter(s => region.value === "all" || s.region_code === region.value) ?? [])
const calendarSources = computed(() => info.value?.calendar_sources.filter(s => (!searchText.value || `${s.name} ${s.code}`.toLowerCase().includes(searchText.value.toLowerCase())) && (statusFilter.value === "all" || (statusFilter.value === "paused" ? !s.enabled : statusFilter.value === "error" ? s.errors.length > 0 || !s.worker_alive : s.enabled && s.worker_alive && !s.errors.length))).filter(s => region.value === "all" || s.region_code === region.value) ?? [])
function exportSources() {
  const rows: unknown[][] = [["类型", "来源", "代码", "启用", "状态", "最近成功（UTC）", "累计收录", "今日新增", "今日修订", "错误"]]
  if (collector.value !== "calendar") rows.push(...newsSources.value.map(source => ["新闻", source.name, source.code, source.enabled, sourceStatus(source, restarting.has(source.code)).text, source.last_success_at, source.total_items, source.today_new, source.today_revised, source.last_error]))
  if (collector.value !== "news") rows.push(...calendarSources.value.map(source => ["经济日历", source.name, source.code, source.enabled, source.worker_alive ? "采集进程在线" : "采集进程离线", source.schedule_success_at, null, null, null, source.errors.join("；")]))
  downloadCsv("sources.csv", rows)
}
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
  <div class="flex flex-wrap gap-2 mb-3">
    <el-input v-model="searchText" placeholder="搜索来源名称或代码" clearable class="w-60" /><el-select v-model="statusFilter" class="w-36">
      <el-option v-for="(label, value) in { all: '全部状态', normal: '正常', error: '失败', paused: '暂停' }" :key="value" :label="label" :value="value" />
    </el-select><el-button @click="searchText = ''; statusFilter = 'all'; region = 'all'; collector = 'all'">
      重置过滤器
    </el-button><el-button @click="exportSources">
      导出采集指标 CSV
    </el-button>
  </div>
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
    <div class="crawler-workspace" :class="{ 'with-detail': wide }">
      <div class="crawler-sources">
        <el-radio-group v-model="sourceView" size="small" class="mb-2">
          <el-radio-button value="cards">
            来源卡片
          </el-radio-button><el-radio-button value="table">
            数据表格
          </el-radio-button>
        </el-radio-group>
        <el-card v-if="collector !== 'calendar'" shadow="never" body-class="p-0">
          <template #header>
            新闻来源 · {{ newsSources.length }}
          </template>
          <div v-if="sourceView === 'cards'" class="source-cards">
            <button v-for="source in newsSources" :key="source.code" class="source-card" :class="{ selected: detailCode === source.code, unhealthy: source.consecutive_failures >= FAILURE_ALERT }" @click="openDetail(source.code, 'news')">
              <div class="flex items-center gap-2">
                <b>{{ source.name }}</b><small>{{ source.code }}</small><div class="flex-1" /><el-tag size="small" :type="sourceStatus(source, restarting.has(source.code)).type">
                  {{ sourceStatus(source, restarting.has(source.code)).text }}
                </el-tag>
              </div>
              <div class="source-metrics">
                <span>今日新增<b>{{ source.today_new }}</b></span><span>今日修订<b>{{ source.today_revised }}</b></span><span>累计收录<b>{{ source.total_items }}</b></span><span>采集频率<b>{{ source.interval_seconds }}s</b></span>
              </div>
              <div class="source-card-note">
                最近成功 {{ source.last_success_at ? formatTime(source.last_success_at) : '尚无成功记录' }}<span v-if="source.consecutive_failures"> · 连续失败 {{ source.consecutive_failures }} 次</span>
              </div>
              <p v-if="source.last_error" class="source-card-error">
                {{ source.last_error }}
              </p><div v-else class="source-card-note">
                {{ source.history_done ? '历史回补完成' : source.history_position ? `历史回补至 ${source.history_position}` : '选择来源查看配置和采集记录' }}
              </div>
            </button><el-empty v-if="!newsSources.length" description="没有符合条件的新闻来源" />
          </div>
          <el-table v-else :data="newsSources" size="small" @row-click="row => openDetail(row.code, 'news')">
            <el-table-column prop="name" label="来源" min-width="170" />
            <el-table-column label="地域" width="95">
              <template #default="{ row }">
                {{ regions[row.region_code as keyof typeof regions] ?? row.region_code }}
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="130">
              <template #default="{ row }">
                <el-tag size="small" :type="sourceStatus(row as SourceInfo, restarting.has(row.code)).type">
                  {{ sourceStatus(row as SourceInfo, restarting.has(row.code)).text }}
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
      </div>
      <aside v-if="wide" class="crawler-detail">
        <h2>{{ newsDetail?.name ?? calendarDetail?.name ?? '采集来源详情' }}</h2><SourcePanel v-if="newsDetail" :key="newsDetail.code" :source="newsDetail" :logs="logs[newsDetail.code] ?? []" :restarting="restarting.has(newsDetail.code)" :alert="newsDetail.consecutive_failures >= FAILURE_ALERT" @restart="restart(newsDetail.code)" @changed="refresh" /><CalendarSourcePanel v-else-if="calendarDetail" :source="calendarDetail" /><el-empty v-else description="选择来源查看状态、配置与最近入库" />
      </aside>
    </div>
    <el-drawer :model-value="!wide && detailVisible" @close="detailVisible = false" :title="newsDetail?.name ?? calendarDetail?.name ?? '来源详情'" size="min(640px, 100vw)">
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
          <el-button size="small" @click="Object.keys(logs).forEach(key => logs[key] = [])">
            清空当前显示
          </el-button>
          <el-button size="small" @click="expandedLogs = !expandedLogs">
            {{ expandedLogs ? "收起" : "展开" }}
          </el-button>
          <el-switch v-model="autoScroll" active-text="自动滚动" size="small" />
        </div>
      </template>
      <div ref="logBox" :style="{ height: expandedLogs ? '75vh' : '400px' }" class="overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-200 font-mono leading-6">
        <div v-for="log in visibleLogs" :key="log.id" class="whitespace-pre-wrap break-all">
          <span class="text-slate-500">{{ formatTime(log.ts) }}</span>
          <span class="mx-2 rounded px-1" :class="levelClass(log.level)">{{ log.level }}</span>
          <span class="mr-2 text-teal-300">[{{ sourceNames[log.source_code] ?? log.source_code }}]</span>{{ log.message }}
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.crawler-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}
.crawler-workspace.with-detail {
  grid-template-columns: minmax(0, 1fr) 400px;
}
.crawler-sources {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.crawler-detail {
  min-width: 0;
  background: white;
  border: 1px solid #e2e8f0;
  border-radius: 5px;
  padding: 14px;
  position: sticky;
  top: 0;
  max-height: 650px;
  overflow: auto;
  font-size: 12px;
}
.crawler-detail h2 {
  margin: 0 0 16px;
  font-size: 15px;
}
.source-cards {
  max-height: 620px;
  overflow: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.source-card {
  text-align: left;
  width: 100%;
  padding: 12px;
  border: 1px solid #e2e8f0;
  border-left: 3px solid #cbd5e1;
  border-radius: 4px;
  cursor: pointer;
  background: white;
  color: #334155;
}
.source-card:hover {
  background: #f8fafc;
}
.source-card.selected {
  border-color: #93b4ff;
  background: #f8faff;
}
.source-card.unhealthy {
  border-left-color: #dc2626;
}
.source-card small {
  color: #94a3b8;
  font-size: 10px;
}
.source-metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  font-size: 10px;
  color: #64748b;
  margin: 10px 0;
}
.source-metrics b {
  display: block;
  color: #334155;
  font-size: 14px;
  margin-top: 3px;
}
.source-card-note {
  color: #64748b;
  font-size: 10px;
  margin-top: 5px;
}
.source-card-error {
  color: #dc2626;
  font-size: 11px;
  margin: 8px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
