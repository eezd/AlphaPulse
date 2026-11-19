<script setup lang="ts">
import type { Analysis, QueueCoverage, Settings } from "@@/apis/briefings"
import {
  getCoverageApi,
  getSettingsApi,
  listAnalysesApi,
  states
} from "@@/apis/briefings"
import { useSessionScope } from "@@/composables/useSessionScope"
import AiQueue from "./components/AiQueue.vue"
import AnalysisDetail from "./components/AnalysisDetail.vue"
import BriefingSettings from "./components/BriefingSettings.vue"

const scope = useSessionScope()
const route = useRoute()
const router = useRouter()
const tab = ref("analyses")
const rows = ref<Analysis[]>([])
const total = ref(0)
const page = ref(1)
const q = ref("")
const state = ref("")
const coverage = ref<QueueCoverage>()
const settings = ref<Settings>()
const failed = ref(false)
const loading = ref(false)
const selected = ref<number | null>(null)
let sequence = 0
let timer: ReturnType<typeof setInterval> | undefined
function open(id: number) {
  selected.value = id
  void router.replace({ path: "/home", query: { analysis: id } })
}
async function load() {
  if (!scope.isCurrent()) return
  const seq = ++sequence
  loading.value = true
  try {
    const [config, counts, result] = await Promise.all([
      getSettingsApi(),
      getCoverageApi(),
      listAnalysesApi({
        page: page.value,
        q: q.value,
        state: state.value || undefined
      })
    ])
    if (!scope.isCurrent() || seq !== sequence) return
    settings.value = config
    coverage.value = counts
    rows.value = result.items
    total.value = result.total
    failed.value = false
  } catch {
    if (scope.isCurrent() && seq === sequence) failed.value = true
  } finally {
    if (scope.isCurrent() && seq === sequence) loading.value = false
  }
}
function search() {
  page.value = 1
  void load()
}
watch(
  () => route.query.analysis,
  (value) => {
    const id = Number(value)
    selected.value = Number.isSafeInteger(id) && id > 0 ? id : null
  },
  { immediate: true }
)
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (!document.hidden && !loading.value && tab.value !== "settings")
      void load()
  }, 15000)
})
onBeforeUnmount(() => {
  sequence++
  clearInterval(timer)
})
</script>

<template>
  <div class="home-page">
    <div class="toolbar">
      <div>
        <h1>新闻分析</h1>
        <p>每条新闻一次摘要、关键词与重要性评价，定时报表复用已有分析。</p>
      </div>
      <div class="toolbar">
        <router-link to="/reports">
          阅读日报与分时报告
        </router-link>
      </div>
    </div>
    <el-alert
      v-if="failed"
      type="error"
      title="数据读取失败，当前展示上次结果"
      :closable="false"
    />
    <el-card v-if="coverage" shadow="never">
      <div class="toolbar">
        <b>{{ settings?.enabled ? "正在处理" : "新流程已暂停" }}</b><span>新闻 {{ coverage.news_total }} · 尚未入队
          {{ coverage.not_enrolled }}</span><span
          v-for="count in coverage.analyses"
          :key="`${count.state}:${count.lane}`"
        >{{ count.lane === "live" ? "实时" : "历史" }}
          {{ states[count.state] ?? count.state }} {{ count.count }}</span>
      </div>
      <p>
        历史报表范围 {{ coverage.history_start }}—{{ coverage.history_end }} ·
        游标 {{ coverage.history_cursor ?? "未启动" }}
      </p>
      <div class="toolbar">
        <span
          v-for="count in coverage.reports"
          :key="`${count.state}:${count.lane}`"
        >报告 {{ count.lane === "live" ? "实时" : "历史" }}
          {{ states[count.state] ?? count.state }} {{ count.count }}</span>
      </div>
    </el-card>
    <el-tabs v-model="tab">
      <el-tab-pane label="单篇分析" name="analyses" /><el-tab-pane
        label="AI 队列"
        name="queue"
      /><el-tab-pane label="分析与报告配置" name="settings" />
    </el-tabs>
    <AiQueue v-if="tab === 'queue'" @analysis="open" />
    <BriefingSettings v-else-if="tab === 'settings'" @changed="load" />
    <template v-else>
      <div class="toolbar">
        <el-input
          v-model="q"
          placeholder="搜索标题、关键词或别名"
          clearable
          @keyup.enter="search"
        /><el-select
          v-model="state"
          placeholder="全部状态"
          clearable
          @change="search"
        >
          <el-option
            v-for="name in [
              'pending',
              'processing',
              'done',
              'failed',
              'interrupted',
            ]"
            :key="name"
            :value="name"
            :label="states[name]"
          />
        </el-select><el-button :loading="loading" @click="search">
          查询
        </el-button>
      </div>
      <el-table :data="rows" stripe @row-click="(row) => open(row.id)">
        <el-table-column label="新闻" min-width="240">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="open(row.id)">
              {{ row.title }}
            </el-button>
            <p v-if="row.source_changed">
              分析对应旧版原文
            </p>
          </template>
        </el-table-column><el-table-column label="状态" width="110">
          <template #default="{ row }">
            {{ states[row.state] ?? row.state }}
          </template>
        </el-table-column><el-table-column label="重要性" width="140">
          <template #default="{ row }">
            {{ row.importance_stars ? "★".repeat(row.importance_stars) : "—" }}
            {{ row.importance_score ?? "" }}
          </template>
        </el-table-column><el-table-column label="关键词" min-width="200">
          <template #default="{ row }">
            <el-tag
              v-for="word in row.keywords"
              :key="word.canonical"
              class="word"
            >
              {{ word.canonical }}
            </el-tag>
          </template>
        </el-table-column><el-table-column label="版本 / 归窗" min-width="150">
          <template #default="{ row }">
            v{{ row.version_no }} ·
            {{ row.time_basis === "first_seen_at" ? "入站时间" : "发布时间" }}
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="page"
        :page-size="20"
        :total="total"
        layout="prev, pager, next, total"
        @current-change="load"
      />
    </template>
    <el-drawer
      :model-value="selected != null"
      title="单篇分析"
      size="min(900px, 100%)"
      destroy-on-close
      @close="router.replace('/home')"
    >
      <AnalysisDetail :id="selected" @version="open" @changed="load" />
    </el-drawer>
  </div>
</template>

<style scoped>
.home-page {
  display: grid;
  gap: 16px;
  padding: 20px;
}
.toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.toolbar .el-input {
  width: min(100%, 320px);
}
.toolbar .el-select {
  width: 180px;
}
.word {
  margin: 3px;
}
h1 {
  margin: 0;
}
p {
  color: var(--el-text-color-secondary);
  line-height: 1.7;
}
:deep(.el-table .el-button > span) {
  white-space: normal;
  text-align: left;
  overflow-wrap: anywhere;
}
</style>
