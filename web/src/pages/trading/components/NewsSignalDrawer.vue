<script lang="ts" setup>
import type { NewsSignalItem, NewsSignals } from "@/apis/trading"
import { getNewsSignals } from "@/apis/trading"
import { useSourceNames } from "@/composables/useSourceNames"
import { formatTime } from "@/utils/text"

const visible = defineModel<boolean>({ required: true })

const CATEGORY_LABELS = { event: "事件", price_report: "行情播报", irrelevant: "无关" } as const
const DIRECTION_LABELS = { bullish: "利多", bearish: "利空", neutral: "中性" } as const
const DIRECTION_TYPES = { bullish: "success", bearish: "danger", neutral: "info" } as const

const sourceNames = useSourceNames()
const data = ref<NewsSignals>()
const loading = ref(false)

watch(visible, async (open) => {
  if (!open)
    return
  loading.value = true
  try {
    data.value = await getNewsSignals()
  } finally {
    loading.value = false
  }
})

// 无关类不交易，只看事件和行情播报
const stats = computed(() => (data.value?.stats ?? []).filter(s => s.category !== "irrelevant"))

function bp(value: number | null) {
  return value === null ? "-" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}`
}

function pnlClass(value: number | null) {
  return value === null ? "" : value > 0 ? "text-emerald-600" : value < 0 ? "text-rose-600" : ""
}

/** 按标注方向取正负后的收益：利多看涨、利空看跌 */
function signed(row: NewsSignalItem, symbol: string) {
  const ret = row.returns?.[symbol]?.["4h"]
  if (ret === undefined || row.direction === "neutral")
    return null
  return row.direction === "bullish" ? ret : -ret
}
</script>

<template>
  <el-drawer v-model="visible" title="新闻信号（前向记录）" size="70%">
    <div v-loading="loading" class="flex flex-col gap-3 text-sm">
      <el-alert type="info" :closable="false">
        <div class="leading-6">
          只记录、不下单：{{ data ? formatTime(data.start, "MM-DD HH:mm") : "-" }} 之后发布的加密相关新闻，由模型标注类别和对 BTC 的方向，
          按标注方向（利多做多、利空做空）在发布后第 5 分钟进场，看持有 1 小时、4 小时的收益；扣费按来回 8bp。
          研究样本里事件新闻 4 小时约 +15bp，但不显著；要确认这个幅度，大约需要 250 条事件信号（约 12 个月）。
        </div>
      </el-alert>
      <div v-if="data" class="text-xs text-slate-500">
        已标注 <b class="num text-slate-900">{{ data.labeled }}</b> 条，其中满 4 小时、已回填价格 <b class="num text-slate-900">{{ data.filled }}</b> 条
        （同一件事 10 分钟内的多条报道只算一次）
      </div>

      <el-table :data="stats" cell-class-name="num" empty-text="还没有满 4 小时的信号">
        <el-table-column label="类别" width="90">
          <template #default="{ row }">
            <span class="font-sans">{{ CATEGORY_LABELS[row.category as keyof typeof CATEGORY_LABELS] }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="symbol" label="币种" width="100" />
        <el-table-column prop="horizon" label="持有" width="70" />
        <el-table-column prop="n" label="笔数" width="70" />
        <el-table-column label="平均（bp）" min-width="90">
          <template #default="{ row }">
            <span :class="pnlClass(row.mean_bp)">{{ bp(row.mean_bp) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="t 值" min-width="70">
          <template #default="{ row }">
            {{ row.t === null ? "-" : row.t.toFixed(2) }}
          </template>
        </el-table-column>
        <el-table-column label="扣费后（bp）" min-width="100">
          <template #default="{ row }">
            <b :class="pnlClass(row.net_bp)">{{ bp(row.net_bp) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="胜率" min-width="70">
          <template #default="{ row }">
            {{ row.win_rate === null ? "-" : `${(row.win_rate * 100).toFixed(1)}%` }}
          </template>
        </el-table-column>
      </el-table>

      <div class="mt-2 text-slate-900 font-semibold">
        最近 50 条
      </div>
      <el-table :data="data?.recent ?? []" empty-text="还没有新的加密新闻">
        <el-table-column label="发布时间" width="120" class-name="num">
          <template #default="{ row }">
            {{ formatTime(row.published_at, "MM-DD HH:mm") }}
          </template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <span class="text-xs text-slate-500">{{ sourceNames[row.source_code] ?? row.source_code }}</span>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.title }}
          </template>
        </el-table-column>
        <el-table-column label="标注" width="120">
          <template #default="{ row }">
            <div class="flex gap-1">
              <el-tag size="small" type="info">
                {{ CATEGORY_LABELS[row.category as keyof typeof CATEGORY_LABELS] }}
              </el-tag>
              <el-tag size="small" :type="DIRECTION_TYPES[row.direction as keyof typeof DIRECTION_TYPES]">
                {{ DIRECTION_LABELS[row.direction as keyof typeof DIRECTION_LABELS] }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-for="symbol in ['BTCUSDT', 'ETHUSDT']" :key="symbol" :label="`${symbol.replace('USDT', '')} 4h（bp）`" width="110" class-name="num">
          <template #default="{ row }">
            <span v-if="!row.returns" class="font-sans text-xs text-slate-400">未满 4 小时</span>
            <span v-else :class="pnlClass(signed(row as NewsSignalItem, symbol))" title="按标注方向取正负：正数表示方向判断对了">
              {{ bp(signed(row as NewsSignalItem, symbol)) }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </el-drawer>
</template>
