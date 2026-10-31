<script lang="ts" setup>
import type { CalendarData, CalendarItem } from "@@/apis/calendar/type"
import { listCalendarApi } from "@@/apis/calendar"
import { useSessionScope } from "@@/composables/useSessionScope"
import { ArrowLeft, ArrowRight, Refresh } from "@element-plus/icons-vue"
import dayjs from "dayjs"
import timezone from "dayjs/plugin/timezone"
import utc from "dayjs/plugin/utc"
import CalendarDetail from "./components/CalendarDetail.vue"
import { flagUrl, forecastNote, sources, timeLabel, value } from "./presentation"

dayjs.extend(utc)
dayjs.extend(timezone)
const zone = "Asia/Shanghai"
const scope = useSessionScope()
const kind = ref<CalendarItem["kind"]>("data")
const today = () => dayjs().tz(zone).format("YYYY-MM-DD")
const selected = ref(today())
const days = ref(7)
const rangeEnd = computed(() => dayjs(selected.value).add(days.value - 1, "day").format("YYYY-MM-DD"))
const data = ref<CalendarData | null>(null)
const loading = ref(false)
const failed = ref(false)
const now = ref(Date.now())
const detail = ref<CalendarItem | null>(null)
const country = ref("")
const impactLabels = { pending: "待公布", bullish: "利多", bearish: "利空", neutral: "影响较小", uncertain: "待判断" }
const countries = computed(() => data.value?.countries ?? [])
const labels = { scheduled: "待公布", waiting: "获取中", delayed: "取数延迟", released: "已公布", withdrawn: "已撤回", error: "采集异常", historical: "仅有日程", unavailable: "官方未公布", recorded: "已收录" }
const formatted = (time: string) => dayjs(time).tz(zone).format("MM-DD HH:mm:ss")
const groups = computed(() => {
  const result = new Map<string, CalendarItem[]>()
  for (const item of data.value?.items ?? []) {
    if (item.kind !== kind.value || (country.value && item.country_code !== country.value)) continue
    const key = item.calendar_date
    const rows = result.get(key) ?? []
    rows.push(item)
    result.set(key, rows)
  }
  return [...result.entries()]
})
const online = computed(() => !!data.value?.worker && now.value - Date.parse(data.value.worker.at) < 30000)
const scheduleErrors = computed(() => Object.values(data.value?.worker?.sources ?? {}).filter(s => s.error || now.value - Date.parse(s.checked_at) > 7200000))
const hasSchedule = computed(() => Object.keys(data.value?.worker?.sources ?? {}).length > 0)
const jin10Online = computed(() => !!data.value?.jin10 && now.value - Date.parse(data.value.jin10.at) < 30000 && ["ready", "syncing"].includes(data.value.jin10.state))
const coveredDays = computed(() => data.value?.coverage.length ?? 0)
const upcoming = computed(() => data.value?.items.find(i => i.kind === kind.value && i.time_precision === "minute" && (!country.value || i.country_code === country.value) && i.status === "scheduled" && Date.parse(i.scheduled_at) > now.value))
const countdown = computed(() => {
  if (!upcoming.value) return ""
  const seconds = Math.max(0, Math.floor((Date.parse(upcoming.value.scheduled_at) - now.value) / 1000))
  return `${Math.floor(seconds / 3600)}时 ${Math.floor(seconds % 3600 / 60)}分 ${seconds % 60}秒`
})
let seq = 0
let timer: ReturnType<typeof setTimeout> | undefined
let clock: ReturnType<typeof setInterval> | undefined
let disposed = false

async function load() {
  if (!scope.isCurrent()) return
  const current = ++seq
  clearTimeout(timer)
  loading.value = data.value === null
  try {
    const result = await listCalendarApi(selected.value, days.value)
    if (disposed || !scope.isCurrent() || current !== seq) return
    data.value = result
    failed.value = false
  } catch {
    // HTTP 层已提示具体错误；保留上次数据并标为失联，不显示为实时成功。
    if (!disposed && scope.isCurrent() && current === seq) failed.value = true
  } finally {
    if (scope.isCurrent() && current === seq && !disposed) {
      loading.value = false
      const due = data.value?.items.some(i => i.time_precision === "minute" && ["scheduled", "waiting", "delayed"].includes(i.status) && Math.abs(Date.parse(i.scheduled_at) - Date.now()) < 900000)
      timer = setTimeout(load, failed.value ? 30000 : due ? 3000 : 30000)
    }
  }
}

function move(direction: number) {
  selected.value = dayjs(selected.value).add(direction * days.value, "day").format("YYYY-MM-DD")
}

function openDetail(item: CalendarItem) {
  detail.value = item
}

watch([selected, days], () => {
  data.value = null
  load()
})
watch(data, () => {
  if (!detail.value) return
  const item = data.value?.items.find(i => i.release_id === detail.value?.release_id && i.indicator === detail.value?.indicator)
  if (item) {
    detail.value = item
  }
})
onMounted(() => {
  load()
  clock = setInterval(() => {
    now.value = Date.now()
  }, 1000)
})
onUnmounted(() => {
  disposed = true
  ++seq
  clearTimeout(timer)
  clearInterval(clock)
})
</script>

<template>
  <div class="calendar-page">
    <div class="calendar-heading">
      <div>
        <h1>经济日历</h1>
        <p>北京时间 UTC+8 · 全球宏观数据、重要事件与假期</p>
      </div>
      <el-tag :type="online && !failed ? 'success' : 'danger'">
        {{ online && !failed ? "采集在线" : "采集或连接离线" }}
      </el-tag>
    </div>
    <el-alert v-if="failed || !online || !hasSchedule || scheduleErrors.length" :closable="false" type="warning" show-icon title="日历同步未就绪或来源异常，当前列表不能用于排除重大事件。" />
    <div v-if="upcoming" class="next-event">
      <span>下个事件</span><strong><img v-if="flagUrl(upcoming)" class="country-flag" :src="flagUrl(upcoming)!" :alt="upcoming.country_name"> {{ upcoming.event_name }}</strong>
      <span>{{ formatted(upcoming.scheduled_at) }}</span><b>{{ countdown }}</b>
    </div>
    <el-card shadow="never" class="calendar-card">
      <div class="calendar-toolbar">
        <el-button :icon="ArrowLeft" circle aria-label="上一段日期" @click="move(-1)" />
        <el-date-picker v-model="selected" type="date" value-format="YYYY-MM-DD" :clearable="false" aria-label="查询起始日期" />
        <el-button :icon="ArrowRight" circle aria-label="下一段日期" @click="move(1)" />
        <el-button @click="selected = today()">
          今天
        </el-button>
        <el-select v-model="country" class="country-select" placeholder="全部国家" aria-label="国家筛选">
          <el-option label="全部国家" value="" />
          <el-option v-for="option in countries" :key="option.code" :label="option.name" :value="option.code" />
        </el-select>
        <el-radio-group v-model="days">
          <el-radio-button :value="1">
            日历
          </el-radio-button>
          <el-radio-button :value="7">
            周历
          </el-radio-button>
        </el-radio-group>
        <el-button :icon="Refresh" :loading="loading" @click="load">
          刷新
        </el-button>
      </div>
      <el-radio-group v-model="kind" aria-label="日历类别">
        <el-radio-button value="data">
          经济数据
        </el-radio-button>
        <el-radio-button value="event">
          重要事件
        </el-radio-button>
        <el-radio-button value="holiday">
          假期休市
        </el-radio-button>
      </el-radio-group>
      <p class="calendar-range" aria-live="polite">
        查询范围：{{ selected }} 至 {{ rangeEnd }}（北京时间，含首尾日期）
      </p>
      <el-alert
        v-if="!jin10Online || coveredDays < days" :closable="false" type="warning" show-icon
        :title="`金十${jin10Online ? '在线' : '同步未就绪'} · 所选 ${days} 天中 ${coveredDays} 天已完整收录三类日历；缺失日期不表示没有事件。`"
      />
      <p v-if="data?.jin10?.error" class="calendar-note">
        {{ data.jin10.error }}
      </p>
      <p class="calendar-note">
        三星标红，四星及以上为主要事件。影响针对 BTC/ETH：优先比较预期，无预期时按前值及利率通道给参考倾向，依据不足显示待判断。金十星级与项目评级分别标注；历史补取预期不视为当时已知。
      </p>
      <div v-loading="loading" class="calendar-results">
        <section v-for="[date, items] in groups" :key="date">
          <h2>{{ date }} <small>{{ items.length }} 项记录</small></h2>
          <el-table :data="items" stripe @row-click="openDetail">
            <el-table-column label="时间" width="90">
              <template #default="{ row }">
                {{ timeLabel(row as CalendarItem) }}
              </template>
            </el-table-column>
            <el-table-column :label="kind === 'data' ? '数据' : kind === 'event' ? '事件' : '假期与休市说明'" min-width="230">
              <template #default="{ row }">
                <div class="event-name" :class="{ 'event-important': row.importance === 3, 'event-major': row.importance >= 4 }">
                  <img v-if="flagUrl(row as CalendarItem)" class="country-flag" :src="flagUrl(row as CalendarItem)!" :alt="row.country_name">
                  <span>{{ row.event_name }}</span>
                  <span v-if="row.importance >= 4" class="major-label">主要</span>
                </div><div class="row-subtitle">
                  {{ sources[row.source] }} · {{ row.title }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="重要性" width="125">
              <template #default="{ row }">
                <span class="stars" :class="{ 'stars-important': row.importance >= 3 }">{{ "★".repeat(row.importance) || "—" }}</span>
                <small class="row-subtitle">{{ row.importance_source === "jin10" ? "金十" : "项目评级" }}</small>
              </template>
            </el-table-column>
            <el-table-column v-if="kind === 'data'" label="前值" width="100">
              <template #default="{ row }">
                {{ value(row.previous, row.unit) }}
              </template>
            </el-table-column>
            <el-table-column v-if="kind === 'data'" label="预期值" width="90">
              <template #default="{ row }">
                {{ value(row.forecast, row.unit) }}
                <small v-if="row.forecast !== null && !row.forecast_before_release" class="row-subtitle">{{ forecastNote(row as CalendarItem) }}</small>
              </template>
            </el-table-column>
            <el-table-column v-if="kind === 'data'" label="公布值" width="110">
              <template #default="{ row }">
                <strong class="actual-value">{{ value(row.actual, row.unit) }}</strong>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="105">
              <template #default="{ row }">
                <el-tag :type="row.status === 'released' ? 'success' : row.status === 'error' ? 'danger' : 'info'">
                  {{ labels[row.status as CalendarItem['status']] }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="kind === 'data'" label="影响 · BTC/ETH" min-width="155">
              <template #default="{ row }">
                <el-tooltip :content="row.explanation" placement="top">
                  <span class="impact-badge" :class="`impact-${row.impact}`">{{ impactLabels[row.impact as CalendarItem['impact']] }}</span>
                </el-tooltip>
                <div v-if="row.actual !== null" class="row-subtitle">
                  {{ row.impact_basis === "forecast" ? "比较预期" : row.impact_basis === "previous" ? "比较前值 · 参考" : "资料不足" }} · 查看依据
                </div>
              </template>
            </el-table-column>
          </el-table>
        </section>
        <el-empty v-if="!loading && groups.length === 0" :description="online && hasSchedule && !scheduleErrors.length && !failed ? '所选日期和国家没有已收录的事件' : '尚无可确认的日历数据'" />
      </div>
      <p class="calendar-note">
        官方站点可能延迟；页面显示实际获取时间，不保证秒级到达。历史日程不等于当时已获取结果，简评不构成交易指令。
      </p>
    </el-card>
    <CalendarDetail v-model="detail" />
  </div>
</template>

<style scoped>
.calendar-page {
  max-width: 1600px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.calendar-heading,
.calendar-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.calendar-heading {
  justify-content: space-between;
}
h1 {
  margin: 0;
  font-size: 20px;
  color: #172b4d;
}
.calendar-heading p,
.calendar-note {
  color: #64748b;
  font-size: 12px;
  line-height: 1.8;
}
.next-event {
  background: #eaf3ff;
  border: 1px solid #d4e4fc;
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  align-items: center;
  color: #355783;
  font-size: 13px;
}
.next-event b {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  color: #2563eb;
}
.calendar-toolbar {
  padding-bottom: 12px;
}
.calendar-range {
  margin: 0;
  color: #172b4d;
  font-size: 14px;
  font-weight: 600;
}
.country-select {
  width: 135px;
}
.country-flag {
  width: 25px;
  height: 16px;
  object-fit: cover;
  border: 1px solid #e2e8f0;
  vertical-align: -3px;
  flex-shrink: 0;
}
.event-name,
.detail-name {
  display: flex;
  align-items: center;
  gap: 7px;
}
.event-name {
  font-weight: 400;
}
.event-important,
.event-major,
.stars-important {
  color: #e64b63;
}
.event-major {
  font-weight: 700;
}
.major-label {
  border: 1px solid #ffc9d1;
  background: #fff0f2;
  color: #d73754;
  font-size: 10px;
  line-height: 18px;
  border-radius: 4px;
  padding: 0 4px;
  white-space: nowrap;
}
.impact-badge {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 5px;
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
}
.impact-bullish {
  background: #fff0f2;
  color: #dc4660;
}
.impact-bearish {
  background: #eaf8f2;
  color: #35916c;
}
.impact-neutral {
  background: #fff7df;
  color: #bc850f;
}
.impact-pending,
.impact-uncertain {
  background: #f0f3f7;
  color: #718096;
}
.calendar-results h2 {
  margin: 20px 0 12px;
  font-size: 16px;
  color: #334155;
}
h2 small {
  font-size: 12px;
  font-weight: normal;
  color: #94a3b8;
  margin-left: 12px;
}
.row-subtitle {
  color: #94a3b8;
  font-size: 11px;
  margin-top: 5px;
  line-height: 1.4;
}
.stars {
  color: #e9a128;
  letter-spacing: 2px;
  white-space: nowrap;
}
.stars.stars-important {
  color: #e64b63;
}
.actual-value {
  color: #172b4d;
  font-variant-numeric: tabular-nums;
}
.revision {
  border-bottom: 1px solid #e2e8f0;
  padding: 12px 0;
  color: #475569;
  font-size: 13px;
}
:deep(.el-table__row) {
  cursor: pointer;
}
:deep(.el-table th.el-table__cell) {
  background: #f4f7fb;
  color: #64748b;
}
:deep(.el-table td.el-table__cell) {
  padding: 8px 0;
}
</style>
