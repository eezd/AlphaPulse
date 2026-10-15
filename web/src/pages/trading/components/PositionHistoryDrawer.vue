<script lang="ts" setup>
import type { PositionHistory } from "@/apis/trading"
import { CLOSE_REASON_LABELS, listPositionHistory } from "@/apis/trading"
import { formatTime } from "@/utils/text"

const emit = defineEmits<{ detail: [id: number] }>()
const visible = defineModel<boolean>({ required: true })

const PAGE = 50

const items = ref<PositionHistory[]>([])
const exhausted = ref(false)
const loading = ref(false)

async function load(more = false) {
  loading.value = true
  try {
    const page = await listPositionHistory({ before: more ? items.value.at(-1)?.analysis_id : undefined, limit: PAGE })
    items.value = more ? [...items.value, ...page] : page
    exhausted.value = page.length < PAGE
  } finally {
    loading.value = false
  }
}

watch(visible, (open) => {
  if (open)
    load()
})

/** 均价保留 7 位有效数字，去掉末尾的 0 */
function price(value: string | null) {
  return value === null ? "-" : String(Number(Number(value).toPrecision(7)))
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
    <el-table v-loading="loading" :data="items" cell-class-name="num" empty-text="还没有平仓的仓位">
      <el-table-column label="币种" min-width="90">
        <template #default="{ row }">
          <span class="text-slate-900 font-semibold">{{ row.symbol }}</span>
        </template>
      </el-table-column>
      <el-table-column label="方向" width="76">
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
      <el-table-column label="仓位价值（USDT）" min-width="110">
        <template #default="{ row }">
          <span v-if="row.quantity" :title="`数量 ${Number(row.quantity)}`">{{ (Number(row.quantity) * Number(row.entry_price)).toFixed(2) }}</span>
          <span v-else>-</span>
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
      <el-table-column label="平仓原因" min-width="120">
        <template #default="{ row }">
          <span class="font-sans">{{ CLOSE_REASON_LABELS[row.close_reason] ?? row.close_reason }}</span>
        </template>
      </el-table-column>
      <el-table-column label="已实现盈亏" min-width="90">
        <template #default="{ row }">
          <span :class="pnlClass(row.realized_pnl)">{{ Number(row.realized_pnl).toFixed(2) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="手续费" min-width="70">
        <template #default="{ row }">
          <span class="text-slate-500">{{ Number(row.commission).toFixed(2) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="净盈亏" min-width="80">
        <template #default="{ row }">
          <b :class="pnlClass(row.net)">{{ Number(row.net).toFixed(2) }}</b>
        </template>
      </el-table-column>
      <el-table-column label="分析" width="80">
        <template #default="{ row }">
          <el-link type="primary" underline="never" title="查看这次计划的分析" @click="emit('detail', row.analysis_id)">
            #{{ row.analysis_id }}
          </el-link>
        </template>
      </el-table-column>
    </el-table>
    <el-button v-if="items.length && !exhausted" link type="primary" class="mt-3" :loading="loading" @click="load(true)">
      加载更早的仓位
    </el-button>
  </el-drawer>
</template>
