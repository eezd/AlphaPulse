<script lang="ts" setup>
import type { Analysis, OrderRow, PlanCondition, TradingSymbol } from "@/apis/trading"
import { CLOSE_REASON_LABELS, PLAN_STATUS_LABELS, TAKE_PROFIT_STEPS } from "@/apis/trading"
import { formatTime } from "@/utils/text"

/** analysis：执行中的计划，没有时为最近一次分析；latest：最近一次分析（与 analysis 不同时另行提示） */
const props = defineProps<{ symbol: TradingSymbol, analysis: Analysis | null, latest: Analysis | null, orders: OrderRow[], requesting: boolean }>()
const emit = defineEmits<{ analyze: [], detail: [id: number] }>()

const ACTION_LABELS = { plan: "新计划", keep: "维持当前计划", exit: "结束当前计划", wait: "观望" } as const
const CONFIDENCE_LABELS = { high: "高", medium: "中", low: "低" } as const
const TIMEFRAME_LABELS = { "mark": "标记价触及", "1m": "1m 收盘", "5m": "5m 收盘", "15m": "15m 收盘", "1h": "1h 收盘", "4h": "4h 收盘" } as const
const ACTIVE_ORDER_STATUSES = ["submitting", "new", "partially_filled"]

const now = ref(Date.now())
const ticker = setInterval(() => now.value = Date.now(), 1000)
onBeforeUnmount(() => clearInterval(ticker))

const decision = computed(() => props.analysis?.decision ?? null)
const sizing = computed(() => props.analysis?.sizing ?? null)
const busy = computed(() => props.requesting || props.symbol.analyzing || !!props.symbol.analysis_requested_at)
const elapsed = computed(() => props.analysis ? Math.max(0, Math.round((now.value - new Date(props.analysis.started_at).getTime()) / 1000)) : 0)

function filled(purpose: "entry" | "tp", leg: number) {
  const total = props.orders.filter(o => o.purpose === purpose && o.leg === leg).reduce((sum, o) => sum + Number(o.filled_qty), 0)
  return total ? String(Number(total.toFixed(8))) : "-"
}

function countdown(deadline: string | null) {
  if (!deadline)
    return ""
  const seconds = Math.round((new Date(deadline).getTime() - now.value) / 1000)
  if (seconds <= 0)
    return "已到期"
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
}

function condition(c: PlanCondition) {
  return `${TIMEFRAME_LABELS[c.timeframe]}${c.direction === "above" ? "高于" : "低于"} ${c.price}`
}

const long = computed(() => sizing.value?.direction === "long")
const entryRow = computed(() => props.orders.find(o => o.purpose === "entry" && Number(o.filled_qty) > 0))
/** 交易所上生效的止损：止盈 1 后会移到保本价 */
const currentStop = computed(() => props.orders.filter(o => o.purpose === "stop" && ACTIVE_ORDER_STATUSES.includes(o.status)).at(-1)?.trigger_price)
const ladder = computed(() => sizing.value?.entry
  ? TAKE_PROFIT_STEPS[sizing.value.direction].map(([pct, ratio], leg) => ({
      leg,
      move: `${long.value ? "+" : "−"}${pct * 100}%`,
      ratio: `${Math.round(ratio * 100)}%`,
      price: props.orders.filter(o => o.purpose === "tp" && o.leg === leg).at(-1)?.price ?? "-",
      filled: filled("tp", leg)
    }))
  : [])
</script>

<template>
  <div class="border border-[var(--el-border-color)] rounded border-solid p-3">
    <div class="mb-2 flex items-center gap-2">
      <span class="font-bold">{{ symbol.symbol }}</span>
      <span class="text-xs text-[var(--el-text-color-secondary)]">短线做波动（5m 确认入场，最长持仓 4 小时）</span>
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
        <el-tag v-if="analysis.plan_status" type="success" effect="dark" size="small">
          {{ PLAN_STATUS_LABELS[analysis.plan_status] }}
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
      <div v-if="latest && latest.id !== analysis.id" class="text-xs text-[var(--el-text-color-secondary)]">
        最近一次分析 #{{ latest.id }}（{{ formatTime(latest.started_at) }}）：
        <template v-if="latest.status === 'done'">
          {{ latest.decision?.summary }}
        </template>
        <template v-else>
          {{ latest.status === "running" ? "分析中" : `失败：${latest.error}` }}
        </template>
        <el-link type="primary" underline="never" class="ml-1 text-xs!" @click="emit('detail', latest.id)">
          查看
        </el-link>
      </div>
      <div v-if="analysis.valid_until || analysis.close_deadline" class="flex flex-wrap gap-x-6">
        <span v-if="analysis.plan_status === 'pending' && analysis.valid_until && !sizing?.entry"><span class="label">等待确认至</span>{{ formatTime(analysis.valid_until, "MM-DD HH:mm") }}（剩 {{ countdown(analysis.valid_until) }}）</span>
        <span v-if="analysis.first_fill_at"><span class="label">入场</span>{{ formatTime(analysis.first_fill_at, "MM-DD HH:mm") }}</span>
        <span v-if="analysis.close_deadline"><span class="label">最晚平仓</span>{{ formatTime(analysis.close_deadline, "MM-DD HH:mm") }}（剩 {{ countdown(analysis.close_deadline) }}）</span>
      </div>
      <div v-if="analysis.result">
        <span class="label">结果</span>{{ CLOSE_REASON_LABELS[analysis.close_reason ?? ""] ?? analysis.close_reason }}，净盈亏 {{ analysis.result.net }} USDT（手续费 {{ analysis.result.commission }}）
      </div>
      <div><span class="label">为什么</span>{{ decision.reasons }}</div>

      <template v-if="decision.action === 'plan' && decision.setup && sizing?.setup">
        <div class="grid grid-cols-2 gap-x-6 gap-y-1">
          <div>
            <span class="label">{{ long ? "支撑区" : "压力区" }}</span>{{ sizing.setup.zone_low }} – {{ sizing.setup.zone_high }} · {{ decision.setup.basis }}
          </div>
          <div>
            <span class="label">确认</span>5m {{ long ? "最低价进区后收盘 ≥" : "最高价进区后收盘 ≤" }} {{ sizing.setup.confirm_price }} → 市价{{ long ? "做多" : "做空" }}；
            先有收盘{{ long ? "低于" : "高于" }} {{ long ? sizing.setup.zone_low : sizing.setup.zone_high }} 则撤销
          </div>
          <div v-if="!sizing.entry">
            <span class="label">预估</span>按确认价入场：止损 {{ sizing.estimate.stop }}（{{ sizing.estimate.stop_pct }}%），数量 {{ sizing.estimate.quantity }}，止损亏损 {{ sizing.estimate.risk_at_stop }} USDT
          </div>
          <div v-if="!sizing.entry">
            <span class="label">有效期</span>{{ decision.valid_hours }} 小时；止损按触发时的极值外 0.5×ATR(5m) 计算，超过 1% 放弃入场
          </div>
        </div>
        <template v-if="sizing.entry">
          <div class="grid grid-cols-2 gap-x-6 gap-y-1">
            <div>
              <span class="label">触发</span>{{ formatTime(sizing.entry.triggered_at, "MM-DD HH:mm:ss") }}，5m 收盘 {{ sizing.entry.bar_close }}，{{ long ? "最低" : "最高" }} {{ sizing.entry.extreme }}，ATR {{ sizing.entry.atr }}
            </div>
            <div>
              <span class="label">入场</span>市价 {{ sizing.entry.quantity }}，成交均价 {{ entryRow?.avg_price ?? "待成交" }}（名义 {{ sizing.entry.notional }} USDT，{{ sizing.entry.leverage }} 倍交易资金）
            </div>
            <div>
              <span class="label">止损</span>{{ currentStop ?? sizing.entry.stop }}（初始 {{ sizing.entry.stop }}，{{ sizing.entry.stop_pct }}%；止盈 1 后移到保本价）
            </div>
            <div>
              <span class="label">止损亏损</span>{{ sizing.entry.risk_at_stop }} USDT（交易资金的 {{ sizing.entry.risk_pct }}%）{{ sizing.entry.capped_by_leverage ? "，受 3 倍上限限制" : "" }}
            </div>
          </div>
          <el-table :data="ladder" border size="small">
            <el-table-column label="分批止盈" min-width="90">
              <template #default="{ row }">
                止盈 {{ row.leg + 1 }}
              </template>
            </el-table-column>
            <el-table-column prop="move" label="距成交均价" min-width="90" />
            <el-table-column prop="ratio" label="平仓比例" min-width="80" />
            <el-table-column prop="price" label="价格" min-width="100" />
            <el-table-column prop="filled" label="已成交" min-width="80" />
          </el-table>
        </template>
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
