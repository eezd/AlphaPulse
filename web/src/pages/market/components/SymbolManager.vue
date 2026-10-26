<script lang="ts" setup>
import type { MarketSymbol } from "@@/apis/market/type"
import { deleteSymbolApi } from "@@/apis/market"
import { useSessionScope } from "@@/composables/useSessionScope"
import { INTERVAL_LABELS } from "../chart"

defineProps<{ symbols: MarketSymbol[] }>()
const emit = defineEmits<{ changed: [] }>()

const sessionScope = useSessionScope()

const visible = defineModel<boolean>({ required: true })

const STATUS = {
  pending: { text: "排队中", type: "info" },
  backfilling: { text: "补齐中", type: "warning" },
  ready: { text: "已就绪", type: "success" },
  error: { text: "出错", type: "danger" },
  delisted: { text: "已下架", type: "info" }
} as const

function overall(s: MarketSymbol) {
  const values = Object.values(s.progress)
  return Math.round(values.reduce((sum, p) => sum + p.percent, 0) / values.length)
}

async function remove(s: MarketSymbol) {
  await ElMessageBox.confirm(`删除 ${s.symbol}（${s.market === "spot" ? "现货" : "永续"}）及其全部 K 线？之后可重新添加并补齐。`, "删除确认", { type: "warning" })
  sessionScope.assertCurrent()
  await deleteSymbolApi(s.id)
  ElMessage.success("已删除")
  emit("changed")
}
</script>

<template>
  <el-drawer v-model="visible" title="交易对管理" size="640px">
    <el-table :data="symbols" row-key="id">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="grid grid-cols-3 gap-x-6 gap-y-1 px-4">
            <div v-for="(p, interval) in row.progress" :key="interval" class="flex items-center gap-2 text-xs">
              <span class="w-12">{{ INTERVAL_LABELS[interval] }}</span>
              <el-progress :percentage="p.percent" :status="p.done ? 'success' : undefined" class="flex-1" />
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="交易对" min-width="120">
        <template #default="{ row }">
          {{ row.base }}/{{ row.quote }}
          <el-tag size="small" class="ml-1">
            {{ row.market === "spot" ? "现货" : "永续" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tooltip :disabled="!row.last_error" :content="row.last_error ?? ''">
            <el-tag :type="STATUS[row.status as keyof typeof STATUS].type" size="small">
              {{ STATUS[row.status as keyof typeof STATUS].text }}
            </el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="补齐进度" min-width="140">
        <template #default="{ row }">
          <el-progress :percentage="overall(row as MarketSymbol)" :status="row.status === 'ready' ? 'success' : undefined" />
        </template>
      </el-table-column>
      <el-table-column width="70">
        <template #default="{ row }">
          <el-button link type="danger" @click="remove(row as MarketSymbol)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-drawer>
</template>
