<script setup lang="ts">
import type { Kind, Notification, Report } from "@@/apis/briefings"
import {
  generateReportApi,
  kinds,
  listNotificationsApi,
  listReportsApi,
  states
} from "@@/apis/briefings"
import { useSessionScope } from "@@/composables/useSessionScope"
import ReportDetail from "./components/ReportDetail.vue"

const scope = useSessionScope()
const route = useRoute()
const router = useRouter()
const tab = ref("reports")
const kind = ref<Kind | "">("")
const dates = ref<[string, string]>()
const region = ref("")
const importance = ref<number>()
const q = ref("")
const page = ref(1)
const total = ref(0)
const rows = ref<Report[]>([])
const notices = ref<Notification[]>([])
const noticeTotal = ref(0)
const noticePage = ref(1)
const loading = ref(false)
const busy = ref(false)
const failed = ref(false)
const selected = ref<number | null>(null)
const generate = reactive({
  kind: "daily" as Kind,
  report_date: "",
  regenerate: false
})
let sequence = 0
let timer: ReturnType<typeof setInterval> | undefined
function open(id: number) {
  void router.replace({ path: "/reports", query: { report: id } })
}
function date(value: string) {
  return new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
}
async function load() {
  if (!scope.isCurrent()) return
  const seq = ++sequence
  loading.value = true
  try {
    const result = await Promise.all([
      listReportsApi({
        page: page.value,
        kind: kind.value || undefined,
        q: q.value || undefined,
        start: dates.value?.[0],
        end: dates.value?.[1],
        region: region.value || undefined,
        stars: importance.value
      }),
      listNotificationsApi(noticePage.value)
    ])
    if (!scope.isCurrent() || seq !== sequence) return
    rows.value = result[0].items
    total.value = result[0].total
    notices.value = result[1].items
    noticeTotal.value = result[1].total
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
async function submit() {
  if (busy.value || !generate.report_date) return
  const body = { ...generate, request_id: crypto.randomUUID() }
  busy.value = true
  try {
    await ElMessageBox.confirm(
      body.regenerate
        ? "将创建新报告版本并重新计算成功步骤，保留全部旧版本。确定？"
        : "创建新版本补报，可复用输入与配置完全相同的成功步骤。确定？",
      "生成 / 补报",
      { type: "warning" }
    )
  } catch {
    if (scope.isCurrent()) busy.value = false
    return
  }
  if (!scope.isCurrent()) return
  try {
    const result = await generateReportApi(body)
    if (scope.isCurrent()) {
      open(result.id)
      await load()
    }
  } catch {
    if (scope.isCurrent()) {
      ElMessage.warning("请求未确认完成，请先刷新并核对报告列表，避免重复生成")
      await load()
    }
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
watch(
  () => route.query.report,
  (value) => {
    const id = Number(value)
    selected.value = Number.isSafeInteger(id) && id > 0 ? id : null
  },
  { immediate: true }
)
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (!document.hidden && !loading.value) void load()
  }, 15000)
})
onBeforeUnmount(() => {
  sequence++
  clearInterval(timer)
})
</script>

<template>
  <main class="reports-page">
    <header>
      <h1>日报</h1>
      <p>
        按国家与市场阅读早报、晚报、盘中报和日报；归属日期与窗口均使用北京时间。
      </p>
      <router-link to="/home">
        新闻分析、AI 队列与配置
      </router-link>
    </header>
    <el-alert
      v-if="failed"
      type="error"
      title="报告或通知记录读取失败，展示上次读取结果"
      :closable="false"
    />
    <el-tabs v-model="tab">
      <el-tab-pane label="报告归档" name="reports" /><el-tab-pane
        label="运行异常通知"
        name="notifications"
      />
    </el-tabs>
    <template v-if="tab === 'reports'">
      <div class="filters">
        <el-select
          v-model="kind"
          clearable
          placeholder="全部报告"
          @change="search"
        >
          <el-option
            v-for="(name, key) in kinds"
            :key="key"
            :value="key"
            :label="name"
          />
        </el-select><el-date-picker
          v-model="dates"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="归属起日"
          end-placeholder="归属止日"
          @change="search"
        /><el-input
          v-model="region"
          placeholder="国家/市场代码，如 US、CRYPTO"
          @keyup.enter="search"
        /><el-select
          v-model="importance"
          clearable
          placeholder="最低星级"
          @change="search"
        >
          <el-option
            v-for="stars in [1, 2, 3, 4, 5]"
            :key="stars"
            :value="stars"
            :label="`${stars} 星以上`"
          />
        </el-select><el-input
          v-model="q"
          clearable
          placeholder="关键词、别名、报告标题"
          @keyup.enter="search"
        /><el-button :loading="loading" @click="search">
          查询
        </el-button>
      </div>
      <el-table :data="rows" stripe @row-click="(row) => open(row.id)">
        <el-table-column label="报告" min-width="260">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="open(row.id)">
              {{ row.title ?? `${row.report_date} ${kinds[row.kind as Kind]}` }}
            </el-button>
            <p>
              {{ row.report_date }} · {{ kinds[row.kind as Kind] }} · v{{
                row.version_no
              }}
              <el-tag
                v-if="row.lane === 'historical_backfill'"
                type="warning"
                size="small"
              >
                历史回放
              </el-tag>
            </p>
          </template>
        </el-table-column><el-table-column label="状态" width="150">
          <template #default="{ row }">
            {{ states[row.state] ?? row.state }}
          </template>
        </el-table-column><el-table-column label="覆盖率 / 缺口" min-width="200">
          <template #default="{ row }">
            {{ row.coverage?.available ?? "—" }}/{{
              row.coverage?.total ?? "—"
            }}
            <p>{{ row.error ?? row.coverage?.reason }}</p>
          </template>
        </el-table-column><el-table-column label="实际生成" min-width="180">
          <template #default="{ row }">
            {{ row.finished_at ? date(row.finished_at) : "尚未完成" }}
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
      <el-card shadow="never">
        <h3>手动生成 / 补报</h3>
        <div class="filters">
          <el-select v-model="generate.kind">
            <el-option
              v-for="(name, key) in kinds"
              :key="key"
              :value="key"
              :label="name"
            />
          </el-select><el-date-picker
            v-model="generate.report_date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="报告归属日期"
          /><el-checkbox v-model="generate.regenerate">
            重新计算成功步骤
          </el-checkbox><el-button
            type="primary"
            :disabled="!generate.report_date"
            :loading="busy"
            @click="submit"
          >
            生成新版本
          </el-button>
        </div>
        <p>
          仅可生成已结束且到达计划检查时间的窗口；缺失和迟到资料通过新版本补报，已发布报告保持不变。
        </p>
      </el-card>
    </template>
    <template v-else>
      <el-alert
        type="info"
        :closable="false"
        title="投递结果未知时请核对 Telegram，系统不会自动重复发送；通知失败不阻断新闻分析。"
      /><el-table :data="notices">
        <el-table-column
          prop="message"
          label="异常"
          min-width="250"
        /><el-table-column label="投递状态" min-width="150">
          <template #default="{ row }">
            {{ states[row.state] ?? row.state }} · {{ row.attempts }} 次
          </template>
        </el-table-column><el-table-column
          prop="error"
          label="原因"
          min-width="180"
        /><el-table-column label="时间" min-width="180">
          <template #default="{ row }">
            {{ date(row.created_at) }}
          </template>
        </el-table-column>
      </el-table><el-pagination
        v-model:current-page="noticePage"
        :page-size="20"
        :total="noticeTotal"
        layout="prev, pager, next, total"
        @current-change="load"
      />
    </template>
    <el-drawer
      :model-value="selected != null"
      title="报告阅读"
      size="min(1100px, 100%)"
      destroy-on-close
      @close="router.replace('/reports')"
    >
      <ReportDetail :id="selected" @version="open" />
    </el-drawer>
  </main>
</template>

<style scoped>
.reports-page {
  display: grid;
  gap: 16px;
  padding: 20px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.filters .el-input,
.filters .el-select {
  width: min(100%, 240px);
}
.filters :deep(.el-date-editor) {
  max-width: 100%;
}
p {
  line-height: 1.8;
  overflow-wrap: anywhere;
}
:deep(.el-table .el-button > span) {
  white-space: normal;
  text-align: left;
}
</style>
