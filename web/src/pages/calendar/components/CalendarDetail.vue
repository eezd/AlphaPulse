<script setup lang="ts">
import type { CalendarHistory, CalendarItem, CalendarRevision, CalendarSeriesValue } from "@@/apis/calendar/type"
import { calendarHistoryApi, calendarRevisionsApi, calendarSeriesApi } from "@@/apis/calendar"
import { useSessionScope } from "@@/composables/useSessionScope"
import dayjs from "dayjs"
import timezone from "dayjs/plugin/timezone"
import utc from "dayjs/plugin/utc"
import { flagUrl, sources, timeLabel, value } from "../presentation"

dayjs.extend(utc)
dayjs.extend(timezone)
const zone = "Asia/Shanghai"
const formatted = (time: string) => dayjs(time).tz(zone).format("MM-DD HH:mm:ss")
const scope = useSessionScope()
const detail = defineModel<CalendarItem | null>({ required: true })
const revisions = ref<CalendarRevision[]>([])
const revisionsLoading = ref(false)
const revisionError = ref(false)
const history = ref<CalendarHistory[]>([])
const historyLoading = ref(false)
const historyError = ref(false)
const series = ref<CalendarSeriesValue[]>([])
const seriesLoading = ref(false)
const seriesError = ref(false)
const detailTab = ref("history")
const revisionValue = (revision: CalendarRevision, item: CalendarItem) => revision.payload.values[revision.payload.values._jin10?.indicator ?? item.indicator]
let detailSeq = 0
let disposed = false
async function load(item: CalendarItem) {
  if (!scope.isCurrent()) return
  const current = ++detailSeq
  revisions.value = []
  history.value = []
  series.value = []
  seriesLoading.value = true
  seriesError.value = false
  revisionsLoading.value = true
  historyLoading.value = true
  revisionError.value = false
  historyError.value = false
  await Promise.allSettled([
    Promise.all([item.release_id, ...(item.related_release_id ? [item.related_release_id] : [])].map(id => calendarRevisionsApi(id))).then((result) => {
      if (!disposed && scope.isCurrent() && current === detailSeq) revisions.value = result.flat().sort((a, b) => Date.parse(b.captured_at) - Date.parse(a.captured_at))
    }).catch(() => {
      if (!disposed && scope.isCurrent() && current === detailSeq) revisionError.value = true
    }).finally(() => {
      if (!disposed && scope.isCurrent() && current === detailSeq) revisionsLoading.value = false
    }),
    (item.kind === "data" ? calendarHistoryApi(item.release_id, item.indicator) : Promise.resolve([])).then((result) => {
      if (!disposed && scope.isCurrent() && current === detailSeq) history.value = result
    }).catch(() => {
      if (!disposed && scope.isCurrent() && current === detailSeq) historyError.value = true
    }).finally(() => {
      if (!disposed && scope.isCurrent() && current === detailSeq) historyLoading.value = false
    }),
    (item.kind === "data" ? calendarSeriesApi(item.release_id, item.indicator) : Promise.resolve([])).then((result) => {
      if (!disposed && scope.isCurrent() && current === detailSeq) series.value = result
    }).catch(() => {
      if (!disposed && scope.isCurrent() && current === detailSeq) seriesError.value = true
    }).finally(() => {
      if (!disposed && scope.isCurrent() && current === detailSeq) seriesLoading.value = false
    })
  ])
}

watch(() => detail.value ? `${detail.value.release_id}:${detail.value.indicator}:${detail.value.revision_no}:${detail.value.related_revision_no ?? ""}` : "", (key, old) => {
  ++detailSeq
  if (!detail.value) return
  if (key.split(":").slice(0, 2).join(":") !== old?.split(":").slice(0, 2).join(":")) detailTab.value = detail.value.kind === "data" ? "history" : "revisions"
  void load(detail.value)
}, { immediate: true })
onBeforeUnmount(() => {
  disposed = true
  ++detailSeq
})
</script>

<template>
  <el-drawer :model-value="!!detail" title="事件详情" size="min(820px, 100vw)" @close="detail = null">
    <template v-if="detail">
      <h2 class="detail-name">
        <img v-if="flagUrl(detail)" class="country-flag" :src="flagUrl(detail)!" :alt="detail.country_name"> {{ detail.event_name }}
      </h2>
      <p>{{ detail.title }}</p>
      <p v-if="detail.period">
        统计期：{{ detail.period }}
      </p>
      <p>日期与时间：{{ detail.calendar_date }} {{ timeLabel(detail) }}（北京时间）</p>
      <p v-if="detail.period_label">
        统计期原文：{{ detail.period_label }}
      </p>
      <template v-if="detail.kind === 'data'">
        <p>公布值：{{ value(detail.actual, detail.unit) }} · 前值：{{ value(detail.previous, detail.unit) }} · 预期：{{ value(detail.forecast, detail.unit) }}</p>
        <p v-if="detail.unit === null">
          来源未标明单位，未自行推定。
        </p>
        <p v-if="detail.previous_revised !== null">
          金十原前值：{{ value(detail.previous_original, detail.unit) }} · 修订前值：{{ value(detail.previous_revised, detail.unit) }}（不代表当期公布值被修订）
        </p>
        <el-alert v-if="detail.forecast !== null && !detail.forecast_before_release" :closable="false" type="warning" title="预期未在发布前有效记录，不能当作公布前已知信息或用于当时的超预期判断。" />
        <p v-if="detail.forecast_observed_at">
          预期首次记录：{{ formatted(detail.forecast_observed_at) }}
        </p>
      </template>
      <p v-for="[field, label] in [['actual', '公布值来源'], ['previous', '前值来源'], ['forecast', '预期来源'], ['scheduled_at', '时间来源']]" :key="field">
        <template v-if="detail.field_sources[field]">
          {{ label }}：<a :href="detail.field_sources[field].url" target="_blank" rel="noopener noreferrer">{{ sources[detail.field_sources[field].source] }}</a>
          <span v-if="detail.field_sources[field].observed_at"> · 获取于 {{ formatted(detail.field_sources[field].observed_at!) }}</span>
        </template>
      </p>
      <p>首次收录：{{ detail.first_received_at ? formatted(detail.first_received_at) : "尚未获取" }}</p>
      <p>最近检查：{{ detail.last_checked_at ? formatted(detail.last_checked_at) : "尚未检查" }}</p>
      <p v-if="detail.previous_basis">
        前值口径：{{ detail.previous_basis }}
      </p>
      <el-alert v-if="detail.error" :closable="false" type="error" :title="detail.error" />
      <el-alert :closable="false" type="info" title="影响简评" :description="detail.explanation" />
      <p><a :href="detail.release_url ?? detail.schedule_url" target="_blank" rel="noopener noreferrer">查看来源 ↗</a></p>
      <el-tabs v-model="detailTab">
        <el-tab-pane v-if="detail.kind === 'data'" label="历次公布值" name="history">
          <p class="calendar-note">
            同一指标、同一发布阶段的已采集记录（含本期）。金十补取值不保证是最初公布版本。首次记录值不保证是原始初值，缺失历史留空；当前官方修订序列在下方独立展示，不能用于还原历史公布时点。
          </p>
          <el-alert v-if="historyError" :closable="false" type="error" title="历史公布值获取失败" />
          <el-table v-loading="historyLoading" :data="history" stripe empty-text="尚无已采集的历史公布值">
            <el-table-column label="统计期" min-width="100">
              <template #default="{ row }">
                {{ row.period ?? row.period_label ?? "—" }}
              </template>
            </el-table-column>
            <el-table-column label="公布时间" min-width="155">
              <template #default="{ row }">
                {{ dayjs(row.scheduled_at).tz(zone).format("YYYY-MM-DD HH:mm") }}
              </template>
            </el-table-column>
            <el-table-column label="前值" width="90">
              <template #default="{ row }">
                {{ value(row.previous, detail.unit) }}
              </template>
            </el-table-column>
            <el-table-column label="预期记录" width="120">
              <template #default="{ row }">
                {{ value(row.forecast, detail.unit) }}
                <small v-if="row.forecast !== null && !row.forecast_before_release"> · 非事前记录</small>
              </template>
            </el-table-column>
            <el-table-column label="首次记录值" width="110">
              <template #default="{ row }">
                {{ value(row.first_recorded_actual, detail.unit) }}
              </template>
            </el-table-column>
            <el-table-column label="本次记录值" width="120">
              <template #default="{ row }">
                <strong>{{ value(row.actual, detail.unit) }}</strong><el-tag v-if="row.revised" size="small" type="warning">
                  已修订
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="80">
              <template #default="{ row }">
                <a v-if="row.release_url" :href="row.release_url" target="_blank" rel="noopener noreferrer">来源 ↗</a>
              </template>
            </el-table-column>
          </el-table>
          <el-alert v-if="seriesError" :closable="false" type="error" title="当前官方历史序列获取失败" />
          <div v-if="seriesLoading || series.length" v-loading="seriesLoading">
            <h3>当前官方历史终值</h3>
            <p class="calendar-note">
              下列数值按官方当前序列单列，可能已修订，与上方当期公告值口径不同；初值事件也不能将这些终值视为当时已知数据。
            </p>
            <el-table :data="series" stripe>
              <el-table-column prop="period" label="统计期" width="100" />
              <el-table-column label="当前终值" width="120">
                <template #default="{ row }">
                  {{ value(row.value, detail.unit) }}
                </template>
              </el-table-column>
              <el-table-column label="采集时间" min-width="155">
                <template #default="{ row }">
                  {{ dayjs(row.captured_at).tz(zone).format("YYYY-MM-DD HH:mm") }}
                </template>
              </el-table-column>
              <el-table-column label="来源" width="80">
                <template #default="{ row }">
                  <a :href="row.source_url" target="_blank" rel="noopener noreferrer">来源 ↗</a>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
        <el-tab-pane label="本期修订记录" name="revisions">
          <el-alert v-if="revisionError" :closable="false" type="error" title="修订记录获取失败" />
          <div v-loading="revisionsLoading">
            <div v-for="r in revisions" :key="`${r.source}:${r.revision_no}`" class="revision">
              <strong>{{ sources[r.source] }} · 版本 {{ r.revision_no }}</strong> · {{ formatted(r.captured_at) }}
              <p>预定时间 {{ r.payload.values._jin10?.precision === "day" ? "全天" : r.payload.values._jin10?.precision === "tentative" ? "待定" : formatted(r.payload.scheduled_at) }}</p>
              <p>{{ r.payload.title }}</p>
              <template v-if="revisionValue(r, detail)">
                <p>公布值 {{ value(revisionValue(r, detail).actual ?? null, detail.unit) }} · 前值 {{ value(revisionValue(r, detail).previous ?? null, detail.unit) }}</p>
                <p>预期 {{ value(revisionValue(r, detail).forecast ?? null, detail.unit) }} · 修订前值 {{ value(revisionValue(r, detail).previous_revised ?? null, detail.unit) }}</p>
              </template>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>
  </el-drawer>
</template>

<style scoped>
.country-flag {
  width: 25px;
  height: 16px;
  object-fit: cover;
  border: 1px solid #e2e8f0;
  vertical-align: -3px;
}
.detail-name {
  display: flex;
  align-items: center;
  gap: 7px;
}
.calendar-note {
  color: #64748b;
  font-size: 12px;
  line-height: 1.8;
}
.revision {
  border-bottom: 1px solid #e2e8f0;
  padding: 12px 0;
  color: #475569;
  font-size: 13px;
}
</style>
