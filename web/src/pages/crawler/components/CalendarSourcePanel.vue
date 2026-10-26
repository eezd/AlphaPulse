<script setup lang="ts">
import type { CalendarSourceInfo } from "@@/apis/crawler/type"
import { formatTime } from "@@/utils/text"

defineProps<{ source: CalendarSourceInfo }>()
const date = (at: string | null) => at ? formatTime(at) : "尚无记录"
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex items-center gap-2">
        <img :src="`/flags/${source.region_code.toLowerCase()}.svg`" alt="" class="h-4 w-6 object-cover">
        <span class="text-sm font-semibold">{{ source.name }}</span>
        <el-tag size="small" type="info">
          日历
        </el-tag>
        <div class="flex-1" />
        <el-tag :type="!source.worker_alive ? 'danger' : source.errors.length ? 'warning' : source.schedule_success_at ? 'success' : 'info'" size="small">
          {{ !source.worker_alive ? "进程离线" : source.errors.length ? "采集异常" : source.schedule_success_at ? "同步正常" : "待同步" }}
        </el-tag>
      </div>
    </template>
    <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-xs text-slate-500">
      <dt>进程心跳</dt><dd>{{ date(source.heartbeat_at) }}</dd>
      <dt>日程最近检查</dt><dd>{{ date(source.schedule_checked_at) }}</dd>
      <dt>日程最近成功</dt><dd>{{ date(source.schedule_success_at) }}</dd>
      <dt>最近取得结果</dt><dd>{{ date(source.latest_result_at) }}</dd>
      <dt>下一场公布</dt><dd>{{ source.next_event_at ? date(source.next_event_at) : "暂未取得后续日程" }}</dd>
      <dt>已到点待结果</dt><dd :class="source.overdue_count ? 'text-amber-600' : ''">
        {{ source.overdue_count }} 场（最近 45 天）
      </dd>
      <dt>已存公告范围</dt>
      <dd>{{ source.history_start ? `${date(source.history_start)} ～ ${date(source.history_end)}` : "尚无当期公告" }}</dd>
    </dl>
    <div v-for="error in source.errors" :key="error" class="mt-2 break-words rounded bg-rose-50 p-2 text-xs text-rose-700">
      {{ error }}
    </div>
    <p class="mt-3 text-xs text-slate-400">
      独立日历进程统一采集；范围仅表示已存首末日期，不代表历史完整。时间为北京时间。
    </p>
  </el-card>
</template>
