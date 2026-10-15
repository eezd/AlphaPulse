<script lang="ts" setup>
import type { Analysis, PlanCondition, TradingSymbol } from "@/apis/trading"
import { ANGLE_LABELS } from "@/apis/trading"
import { formatTime } from "@/utils/text"

const props = defineProps<{ symbol: TradingSymbol, analysis: Analysis | null, requesting: boolean }>()
const emit = defineEmits<{ analyze: [], detail: [id: number] }>()

const ACTION_LABELS = { plan: "新计划", keep: "维持当前计划", exit: "结束当前计划", wait: "观望" } as const
const CONFIDENCE_LABELS = { high: "高", medium: "中", low: "低" } as const
const TIMEFRAME_LABELS = { "mark": "标记价触及", "1m": "1m 收盘", "15m": "15m 收盘", "1h": "1h 收盘", "4h": "4h 收盘" } as const

const now = ref(Date.now())
const ticker = setInterval(() => now.value = Date.now(), 1000)
onBeforeUnmount(() => clearInterval(ticker))

const decision = computed(() => props.analysis?.decision ?? null)
const sizing = computed(() => props.analysis?.sizing ?? null)
const busy = computed(() => props.requesting || props.symbol.analyzing || !!props.symbol.analysis_requested_at)
const elapsed = computed(() => props.analysis ? Math.max(0, Math.round((now.value - new Date(props.analysis.started_at).getTime()) / 1000)) : 0)

function condition(c: PlanCondition) {
  return `${TIMEFRAME_LABELS[c.timeframe]}${c.direction === "above" ? "高于" : "低于"} ${c.price}`
}

/** 失效后的应急动作：短线立即市价平仓，长线等 4h 收盘后请模型复核 */
const invalidationAction = computed(() => props.analysis?.angle === "short" ? "立即市价平仓" : "请模型复核")
</script>

<template>
  <div class="border border-[var(--el-border-color)] rounded border-solid p-3">
    <div class="mb-2 flex items-center gap-2">
      <span class="font-bold">{{ symbol.symbol }}</span>
      <span class="text-xs text-[var(--el-text-color-secondary)]">{{ ANGLE_LABELS[symbol.angle] }}</span>
      <div class="flex-1" />
      <el-button v-if="analysis && analysis.status !== 'running'" link type="primary" @click="emit('detail', analysis.id)">
        查看完整分析
      </el-button>
      <el-button size="small" type="primary" :loading="busy" @click="emit('analyze')">
        {{ busy ? "分析中" : "立即分析" }}
      </el-button>
    </div>

    <div v-if="!analysis" class="text-sm text-[var(--el-text-color-secondary)]">
      还没有分析。引擎停止时点「立即分析」为试运行：计划只记录，不下单。
    </div>
    <div v-else-if="analysis.status === 'running'" class="text-sm text-[var(--el-text-color-secondary)]">
      模型分析中，已用 {{ elapsed }} 秒（通常 1–3 分钟）…
    </div>
    <el-alert v-else-if="analysis.status === 'failed'" type="error" :closable="false" show-icon>
      {{ formatTime(analysis.started_at) }} 分析失败：{{ analysis.error }}
    </el-alert>

    <div v-else-if="decision" class="flex flex-col gap-2 text-sm">
      <div class="flex flex-wrap items-center gap-2">
        <el-tag v-if="analysis.dry_run" type="info" size="small">
          试运行
        </el-tag>
        <el-tag size="small">
          {{ ACTION_LABELS[decision.action] }}
        </el-tag>
        <el-tag v-if="decision.direction" :type="decision.direction === 'long' ? 'success' : 'danger'" size="small">
          {{ decision.direction === "long" ? "做多" : "做空" }}
        </el-tag>
        <el-tag v-if="decision.confidence" type="warning" size="small">
          置信度{{ CONFIDENCE_LABELS[decision.confidence] }}
        </el-tag>
        <span class="text-xs text-[var(--el-text-color-secondary)]">
          {{ formatTime(analysis.started_at) }} · {{ analysis.reasons.join("；") }} · {{ analysis.model }}{{ analysis.reasoning_effort ? ` / ${analysis.reasoning_effort}` : "" }}
          · {{ analysis.usage?.input_tokens.toLocaleString() }} / {{ analysis.usage?.output_tokens.toLocaleString() }} token
        </span>
      </div>
      <div class="font-bold">
        {{ decision.summary }}
      </div>
      <div><span class="label">为什么</span>{{ decision.reasons }}</div>

      <template v-if="decision.action === 'plan' && sizing">
        <el-table :data="sizing.entries" border size="small">
          <el-table-column label="入场档位" min-width="110">
            <template #default="{ $index }">
              第 {{ $index + 1 }} 档
            </template>
          </el-table-column>
          <el-table-column prop="price" label="挂单价" min-width="100" />
          <el-table-column label="比例" width="80">
            <template #default="{ row }">
              {{ Math.round(Number(row.ratio) * 100) }}%
            </template>
          </el-table-column>
          <el-table-column prop="quantity" label="数量" min-width="90" />
          <el-table-column prop="notional" label="名义 USDT" min-width="100" />
          <el-table-column label="依据" min-width="200">
            <template #default="{ $index }">
              {{ decision.entries?.[$index]?.basis }}
            </template>
          </el-table-column>
        </el-table>
        <el-table :data="sizing.take_profits" border size="small">
          <el-table-column label="分批止盈" min-width="110">
            <template #default="{ $index }">
              止盈 {{ $index + 1 }}
            </template>
          </el-table-column>
          <el-table-column prop="price" label="价格" min-width="100" />
          <el-table-column label="平仓比例" width="80">
            <template #default="{ row }">
              {{ Math.round(Number(row.ratio) * 100) }}%
            </template>
          </el-table-column>
          <el-table-column prop="quantity" label="数量" min-width="90" />
          <el-table-column label="依据" min-width="300">
            <template #default="{ $index }">
              {{ decision.take_profits?.[$index]?.basis }}
            </template>
          </el-table-column>
        </el-table>
        <div class="grid grid-cols-2 gap-x-6 gap-y-1">
          <div><span class="label">止损</span>{{ sizing.stop }}（交易所挂单，标记价触发）· {{ decision.stop_basis }}</div>
          <div>
            <span class="label">失效条件</span>{{ decision.invalidation ? condition(decision.invalidation) : "-" }}
            → {{ invalidationAction }} · {{ decision.invalidation?.basis }}
          </div>
          <div><span class="label">均价 / 数量</span>{{ sizing.average_price }} / {{ sizing.quantity }}（名义 {{ sizing.notional }} USDT，{{ sizing.leverage }} 倍交易资金）</div>
          <div><span class="label">止损亏损</span>{{ sizing.risk_at_stop }} USDT（交易资金的 {{ sizing.risk_pct }}%）{{ sizing.capped_by_leverage ? "，受 3 倍上限限制" : "" }}</div>
          <div><span class="label">盈亏比</span>全部成交 {{ sizing.reward_risk }}，仅首档 {{ sizing.reward_risk_first_only }}</div>
          <div><span class="label">时效</span>挂单 {{ decision.entry_valid_hours }} 小时内有效，首档成交后最长持仓 {{ decision.max_hold_hours }} 小时</div>
        </div>
      </template>

      <div v-if="decision.reevaluate.length">
        <span class="label">重新判断</span>
        <div v-for="(c, i) in decision.reevaluate" :key="i" class="ml-2">
          · {{ condition(c) }}：{{ c.basis }}
        </div>
      </div>
      <div><span class="label">风险</span>{{ decision.risks }}</div>
    </div>
  </div>
</template>

<style scoped>
.label {
  margin-right: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
