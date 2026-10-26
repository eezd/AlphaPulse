<script setup lang="ts">
import type { CalendarHistory, CalendarItem, CalendarRevision, CalendarSeriesValue } from "@@/apis/calendar/type"
import { calendarHistoryApi, calendarRevisionsApi, calendarSeriesApi } from "@@/apis/calendar"
import dayjs from "dayjs"
import timezone from "dayjs/plugin/timezone"
import utc from "dayjs/plugin/utc"

dayjs.extend(utc)
dayjs.extend(timezone)
const zone = "Asia/Shanghai"
const formatted = (time: string) => dayjs(time).tz(zone).format("MM-DD HH:mm:ss")
const value = (v: string | null, unit: string) => v === null ? "—" : `${v}${unit}`
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
let detailSeq = 0
let disposed = false
async function load(item: CalendarItem) {
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
    calendarRevisionsApi(item.release_id).then((result) => {
      if (!disposed && current === detailSeq) revisions.value = result
    }).catch(() => {
      if (!disposed && current === detailSeq) revisionError.value = true
    }).finally(() => {
      if (!disposed && current === detailSeq) revisionsLoading.value = false
    }),
    calendarHistoryApi(item.release_id, item.indicator).then((result) => {
      if (!disposed && current === detailSeq) history.value = result
    }).catch(() => {
      if (!disposed && current === detailSeq) historyError.value = true
    }).finally(() => {
      if (!disposed && current === detailSeq) historyLoading.value = false
    }),
    calendarSeriesApi(item.release_id, item.indicator).then((result) => {
      if (!disposed && current === detailSeq) series.value = result
    }).catch(() => {
      if (!disposed && current === detailSeq) seriesError.value = true
    }).finally(() => {
      if (!disposed && current === detailSeq) seriesLoading.value = false
    })
  ])
}

watch(() => detail.value ? `${detail.value.release_id}:${detail.value.indicator}:${detail.value.revision_no}` : "", (key, old) => {
  ++detailSeq
  if (!detail.value) return
  if (key.split(":").slice(0, 2).join(":") !== old?.split(":").slice(0, 2).join(":")) detailTab.value = "history"
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
        <img class="country-flag" :src="`/flags/${detail.country_code.toLowerCase()}.svg`" :alt="detail.country_name"> {{ detail.event_name }}
      </h2>
      <p>{{ detail.title }}</p>
      <p v-if="detail.period">
        统计期：{{ detail.period }}
      </p>
      <p>预定公布：{{ formatted(detail.scheduled_at) }}</p>
      <p>首次获取：{{ detail.first_received_at ? formatted(detail.first_received_at) : "尚未获取" }}</p>
      <p>最近检查：{{ detail.last_checked_at ? formatted(detail.last_checked_at) : "尚未检查" }}</p>
      <p v-if="detail.previous_basis">
        前值口径：{{ detail.previous_basis }}
      </p>
      <el-alert v-if="detail.error" :closable="false" type="error" :title="detail.error" />
      <el-alert :closable="false" type="info" title="影响简评" :description="detail.explanation" />
      <p><a :href="detail.release_url ?? detail.schedule_url" target="_blank" rel="noopener noreferrer">查看官方来源 ↗</a></p>
      <el-tabs v-model="detailTab">
        <el-tab-pane label="历次公布值" name="history">
          <p class="calendar-note">
            同一指标、同一发布阶段的当期公告值（含本期）。首次记录值不保证是原始初值，缺失历史留空；当前官方修订序列在下方独立展示，不能用于还原历史公布时点。
          </p>
          <el-alert v-if="historyError" :closable="false" type="error" title="历史公布值获取失败" />
          <el-table v-loading="historyLoading" :data="history" stripe empty-text="尚无已采集的历史公布值">
            <el-table-column prop="period" label="统计期" min-width="100" />
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
            <el-table-column label="首次记录值" width="110">
              <template #default="{ row }">
                {{ value(row.first_recorded_actual, detail.unit) }}
              </template>
            </el-table-column>
            <el-table-column label="当期公告值" width="120">
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
            <div v-for="r in revisions" :key="r.revision_no" class="revision">
              <strong>版本 {{ r.revision_no }}</strong> · {{ formatted(r.captured_at) }}
              <p>预定时间 {{ formatted(r.payload.scheduled_at) }}</p>
              <p v-if="r.payload.values[detail.indicator]">
                公布值 {{ value(r.payload.values[detail.indicator].actual, detail.unit) }}
              </p>
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
