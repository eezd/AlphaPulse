<script lang="ts" setup>
import type { PositionHistory, PositionHistoryPage } from "@/apis/trading"
import dayjs from "dayjs"
import { CLOSE_REASON_LABELS, listPositionHistory, listPositionHistorySymbols } from "@/apis/trading"
import { formatTime } from "@/utils/text"

const emit = defineEmits<{ detail: [id: number] }>()
const visible = defineModel<boolean>({ required: true })

const PAGE = 50
const DATE_SHORTCUTS = [
  { text: "今日", value: () => [dayjs().startOf("day").toDate(), new Date()] },
  { text: "近 7 天", value: () => [dayjs().subtract(6, "day").startOf("day").toDate(), new Date()] },
  { text: "近 30 天", value: () => [dayjs().subtract(29, "day").startOf("day").toDate(), new Date()] }
]

const filters = reactive({
  symbol: undefined as string | undefined,
  range: null as [Date, Date] | null,
  outcome: "" as "" | "profit" | "loss",
  source: "" as "" | "program" | "manual"
})
const page = ref(1)
const result = ref<PositionHistoryPage>()
const symbols = ref<string[]>([])
const loading = ref(false)
// 快速切换筛选时只采用最后一次请求的结果
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    const data = await listPositionHistory({
      symbol: filters.symbol,
      // 日期范围按自然日：结束日当天整天都算
      start: filters.range ? dayjs(filters.range[0]).startOf("day").toISOString() : undefined,
      end: filters.range ? dayjs(filters.range[1]).startOf("day").add(1, "day").toISOString() : undefined,
      outcome: filters.outcome || undefined,
      source: filters.source || undefined,
      page: page.value,
      limit: PAGE
    })
    if (seq === requestSeq)
      result.value = data
  } finally {
    if (seq === requestSeq)
      loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

watch(visible, async (open) => {
  if (!open)
    return
  search()
  symbols.value = await listPositionHistorySymbols()
})

const winRate = computed(() => {
  const s = result.value?.summary
  return s?.count ? `${Math.round(s.wins / s.count * 1000) / 10}%` : "-"
})

/** 均价保留 7 位有效数字，去掉末尾的 0 */
function price(value: string) {
  return String(Number(Number(value).toPrecision(7)))
}

function holding(row: PositionHistory) {
  const seconds = Math.round((new Date(row.closed_at).getTime() - new Date(row.opened_at).getTime()) / 1000)
  if (seconds < 60)
    return `${seconds}s`
  const minutes = Math.round(seconds / 60)
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function pnlClass(value: string) {
  const n = Number(value)
  return n > 0 ? "text-emerald-600" : n < 0 ? "text-rose-600" : ""
}
</script>

<template>
  <el-drawer v-model="visible" title="历史仓位" size="90%">
    <div class="flex flex-col gap-3">
      <div class="flex flex-wrap items-center gap-3">
        <el-select v-model="filters.symbol" placeholder="全部币种" clearable class="w-40" @change="search">
          <el-option v-for="s in symbols" :key="s" :value="s" :label="s" />
        </el-select>
        <el-date-picker
          v-model="filters.range" type="daterange" start-placeholder="平仓开始日" end-placeholder="平仓结束日"
          :shortcuts="DATE_SHORTCUTS" class="max-w-72" @change="search"
        />
        <el-radio-group v-model="filters.outcome" @change="search">
          <el-radio-button value="">
            全部
          </el-radio-button>
          <el-radio-button value="profit">
            盈利
          </el-radio-button>
          <el-radio-button value="loss">
            亏损
          </el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="filters.source" @change="search">
          <el-radio-button value="">
            全部来源
          </el-radio-button>
          <el-radio-button value="program">
            程序
          </el-radio-button>
          <el-radio-button value="manual">
            手动
          </el-radio-button>
        </el-radio-group>
      </div>

      <div v-if="result" class="flex flex-wrap gap-x-8 gap-y-1 border border-slate-200 rounded-lg border-solid bg-slate-50 px-4 py-3 text-xs text-slate-500">
        <span>笔数 <b class="num ml-1 text-sm text-slate-900">{{ result.summary.count }}</b></span>
        <span>盈利 <b class="num ml-1 text-sm text-slate-900">{{ result.summary.wins }}</b></span>
        <span>胜率 <b class="num ml-1 text-sm text-slate-900">{{ winRate }}</b></span>
        <span>净盈亏 <b class="num ml-1 text-sm" :class="pnlClass(result.summary.net)">{{ Number(result.summary.net).toFixed(2) }} USDT</b></span>
        <span>手续费 <b class="num ml-1 text-sm text-slate-900">{{ Number(result.summary.commission).toFixed(2) }} USDT</b></span>
      </div>

      <el-table v-loading="loading" :data="result?.items ?? []" cell-class-name="num" empty-text="没有符合条件的仓位">
        <el-table-column label="币种" min-width="84">
          <template #default="{ row }">
            <span class="text-slate-900 font-semibold">{{ row.symbol }}</span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="64">
          <template #default="{ row }">
            <el-tag :type="row.source === 'program' ? 'primary' : 'warning'" size="small">
              {{ row.source === "program" ? "程序" : "手动" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="方向" width="70">
          <template #default="{ row }">
            <el-tag :type="row.direction === 'long' ? 'success' : 'danger'" size="small">
              {{ row.direction === "long" ? "↑ 多" : "↓ 空" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="开仓时间" width="126">
          <template #default="{ row }">
            {{ formatTime(row.opened_at, "MM-DD HH:mm") }}
          </template>
        </el-table-column>
        <el-table-column label="平仓时间" width="126">
          <template #default="{ row }">
            {{ formatTime(row.closed_at, "MM-DD HH:mm") }}
          </template>
        </el-table-column>
        <el-table-column label="持仓时长" width="80">
          <template #default="{ row }">
            {{ holding(row as PositionHistory) }}
          </template>
        </el-table-column>
        <el-table-column label="仓位价值（USDT）" min-width="100">
          <template #default="{ row }">
            <span :title="`数量 ${Number(row.quantity)}`">{{ (Number(row.quantity) * Number(row.entry_price)).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="开仓均价" min-width="100">
          <template #default="{ row }">
            {{ price(row.entry_price) }}
          </template>
        </el-table-column>
        <el-table-column label="平仓均价" min-width="100">
          <template #default="{ row }">
            {{ price(row.exit_price) }}
          </template>
        </el-table-column>
        <el-table-column label="平仓原因" min-width="112">
          <template #default="{ row }">
            <span class="font-sans">{{ row.close_reason ? CLOSE_REASON_LABELS[row.close_reason] ?? row.close_reason : "手动" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已实现盈亏" min-width="84">
          <template #default="{ row }">
            <span :class="pnlClass(row.realized_pnl)">{{ Number(row.realized_pnl).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="手续费" min-width="60">
          <template #default="{ row }">
            <span class="text-slate-500">{{ Number(row.commission).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="净盈亏" min-width="80">
          <template #default="{ row }">
            <b :class="pnlClass(row.net)">{{ Number(row.net).toFixed(2) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="分析" width="70">
          <template #default="{ row }">
            <el-link v-if="row.analysis_id" type="primary" underline="never" title="查看这次计划的分析" @click="emit('detail', row.analysis_id)">
              #{{ row.analysis_id }}
            </el-link>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-if="result && result.total > PAGE" v-model:current-page="page" :page-size="PAGE" :total="result.total"
        layout="total, prev, pager, next" class="justify-end" @current-change="load"
      />
    </div>
  </el-drawer>
</template>
