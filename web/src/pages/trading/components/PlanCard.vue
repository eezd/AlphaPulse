<script lang="ts" setup>
import type { Analysis, OrderRow, PlanCondition, TradingSymbol } from "@/apis/trading"
import { Clock, Cpu, Refresh, TopRight, Warning } from "@element-plus/icons-vue"
import { CLOSE_REASON_LABELS, PLAN_STATUS_LABELS, TAKE_PROFIT_STEPS } from "@/apis/trading"
import { formatDuration, formatTime } from "@/utils/text"

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
const duration = computed(() => props.analysis?.finished_at ? formatDuration(props.analysis.started_at, props.analysis.finished_at) : "")

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
const ladder = computed(() => sizing.value?.entry && entryRow.value
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
  <div class="border border-ap-line border-solid bg-ap-panel-low p-4">
    <div class="mb-3 flex items-center gap-2 border-b border-b-ap-line border-b-solid pb-2.5">
      <el-icon class="text-ap-brand" :size="18">
        <Cpu />
      </el-icon>
      <span class="font-head text-base font-bold">{{ symbol.symbol }}</span>
      <span class="text-xs text-ap-muted">短线做波动（区间内挂限价单，最长持仓 4 小时）</span>
      <div class="flex-1" />
      <el-link v-if="analysis && analysis.status !== 'running'" type="primary" @click="emit('detail', analysis.id)">
        查看完整分析
        <el-icon class="ml-0.5">
          <TopRight />
        </el-icon>
      </el-link>
      <el-button type="primary" :loading="busy" @click="emit('analyze')">
        {{ busy ? "分析中" : "立即分析" }}
      </el-button>
    </div>

    <div v-if="!analysis" class="state-strip">
      还没有分析。引擎停止时点「立即分析」为试运行：计划只记录，不下单。
    </div>
    <div v-else-if="analysis.status === 'running'" class="state-strip">
      <i class="mr-2.5 inline-block h-1.5 w-1.5 animate-ping bg-ap-brand" />模型分析中，已用 {{ elapsed }} 秒（通常 1–3 分钟）…
    </div>
    <el-alert v-else-if="analysis.status === 'failed'" type="error" :closable="false" show-icon>
      {{ formatTime(analysis.started_at) }} 分析失败：{{ analysis.error }}
    </el-alert>

    <div v-else-if="decision" class="flex flex-col gap-3">
      <div class="flex flex-wrap items-center gap-2">
        <el-tag v-if="analysis.dry_run" type="info">
          试运行
        </el-tag>
        <el-tag v-if="analysis.plan_status" type="success">
          {{ PLAN_STATUS_LABELS[analysis.plan_status] }}
        </el-tag>
        <el-tag type="primary">
          {{ ACTION_LABELS[decision.action] }}
        </el-tag>
        <el-tag v-if="decision.direction" :type="decision.direction === 'long' ? 'success' : 'danger'">
          {{ decision.direction === "long" ? "做多" : "做空" }}
        </el-tag>
        <el-tag v-if="decision.confidence" type="warning">
          置信度{{ CONFIDENCE_LABELS[decision.confidence] }}
        </el-tag>
        <span class="text-[11px] text-ap-muted">
          {{ formatTime(analysis.started_at) }} · {{ analysis.reasons.join("；") }} · {{ analysis.model }}{{ analysis.reasoning_effort ? ` / ${analysis.reasoning_effort}` : "" }}
          {{ duration ? `· 耗时 ${duration}` : "" }}
          · {{ analysis.usage?.input_tokens.toLocaleString() }} / {{ analysis.usage?.output_tokens.toLocaleString() }} token
        </span>
      </div>

      <div class="border-l-4 border-l-ap-brand border-l-solid bg-ap-panel px-3 py-2.5 font-sans text-[15px] font-bold">
        {{ decision.summary }}
      </div>

      <div v-if="latest && latest.id !== analysis.id" class="text-xs text-ap-muted">
        最近一次分析 #{{ latest.id }}（{{ formatTime(latest.started_at) }}）：
        <template v-if="latest.status === 'done'">
          {{ latest.decision?.summary }}
        </template>
        <template v-else>
          {{ latest.status === "running" ? "分析中" : `失败：${latest.error}` }}
        </template>
        <el-link type="primary" class="ml-1 text-xs!" @click="emit('detail', latest.id)">
          查看
        </el-link>
      </div>

      <div v-if="analysis.valid_until || analysis.first_fill_at || analysis.close_deadline" class="flex flex-wrap items-center gap-x-5 gap-y-1 border border-ap-line border-solid bg-ap-sunken px-3 py-1.5 text-xs">
        <el-icon class="text-ap-up" :size="15">
          <Clock />
        </el-icon>
        <span v-if="analysis.plan_status === 'pending' && analysis.valid_until">
          限价单有效至 {{ formatTime(analysis.valid_until, "MM-DD HH:mm") }}<b class="ml-1 text-ap-brand">（剩 {{ countdown(analysis.valid_until) }}）</b>
        </span>
        <span v-if="analysis.first_fill_at">入场 {{ formatTime(analysis.first_fill_at, "MM-DD HH:mm") }}</span>
        <span v-if="analysis.close_deadline">
          最晚平仓 {{ formatTime(analysis.close_deadline, "MM-DD HH:mm") }}<b class="ml-1 text-ap-brand">（剩 {{ countdown(analysis.close_deadline) }}）</b>
        </span>
      </div>

      <div v-if="analysis.result" class="text-sm">
        <span class="label">结果</span>{{ CLOSE_REASON_LABELS[analysis.close_reason ?? ""] ?? analysis.close_reason }}，净盈亏
        <b :class="Number(analysis.result.net) >= 0 ? 'text-ap-up' : 'text-ap-down'">{{ analysis.result.net }} USDT</b>（手续费 {{ analysis.result.commission }}）
      </div>

      <div class="font-sans text-[13px] leading-relaxed">
        <span class="label">为什么</span>{{ decision.reasons }}
      </div>

      <template v-if="decision.action === 'plan' && decision.setup && sizing?.entry">
        <div class="grid grid-cols-2 border border-ap-line border-solid bg-ap-panel-high text-xs">
          <div class="flex flex-col gap-1.5 p-3">
            <div class="param">
              <span>{{ long ? "支撑区" : "压力区" }}</span>
              <b :class="long ? 'text-ap-up' : 'text-ap-down'">{{ sizing.setup.zone_low }} – {{ sizing.setup.zone_high }}</b>
            </div>
            <div class="font-sans text-[11px] text-ap-muted">
              {{ decision.setup.basis }}
            </div>
            <div class="param">
              <span>限价{{ long ? "做多" : "做空" }}</span>
              <b>{{ sizing.entry.price }} × {{ sizing.entry.quantity }}</b>
            </div>
            <div class="text-[11px] text-ap-muted">
              已成交 {{ filled("entry", 0) }}{{ entryRow?.avg_price ? `，均价 ${entryRow.avg_price}` : "" }}；名义 {{ sizing.entry.notional }} USDT，{{ sizing.entry.leverage }} 倍交易资金
            </div>
          </div>
          <div class="flex flex-col gap-1.5 border-l border-l-ap-line border-l-solid p-3">
            <div class="param">
              <span>止损</span>
              <b class="text-ap-down">{{ currentStop ?? sizing.entry.stop }}</b>
            </div>
            <div class="param">
              <span>初始止损 / 距挂单价</span>
              <span>{{ sizing.entry.stop }} / {{ sizing.entry.stop_pct }}%</span>
            </div>
            <div class="param">
              <span>止损亏损</span>
              <b class="text-ap-down">{{ sizing.entry.risk_at_stop }} USDT（交易资金的 {{ sizing.entry.risk_pct }}%）</b>
            </div>
            <div class="text-[11px] text-ap-muted">
              区间远端外 0.5×ATR(5m)、距挂单价至少 1×ATR(5m)；止盈 1 后移到保本价{{ sizing.entry.capped_by_leverage ? "；受 3 倍上限限制" : "" }}
            </div>
          </div>
        </div>

        <div v-if="ladder.length">
          <div class="panel-title mb-1.5">
            分批止盈
          </div>
          <el-table :data="ladder" border size="small">
            <el-table-column label="分批止盈" min-width="90">
              <template #default="{ row }">
                <b class="text-ap-up">止盈 {{ row.leg + 1 }}</b>
              </template>
            </el-table-column>
            <el-table-column prop="move" label="距成交均价" min-width="90" align="right" />
            <el-table-column prop="ratio" label="平仓比例" min-width="80" align="right" />
            <el-table-column label="价格" min-width="100" align="right">
              <template #default="{ row }">
                <b>{{ row.price }}</b>
              </template>
            </el-table-column>
            <el-table-column prop="filled" label="已成交" min-width="80" align="center" />
          </el-table>
        </div>
      </template>

      <div class="flex flex-col gap-1.5 border border-ap-line border-solid bg-ap-sunken p-2.5 font-sans text-xs">
        <div v-for="(c, i) in decision.reevaluate" :key="i" class="flex items-start gap-1.5 text-ap-brand">
          <el-icon class="mt-0.5 shrink-0">
            <Refresh />
          </el-icon>
          <span><b>重新判断：</b>{{ condition(c) }}：{{ c.basis }}</span>
        </div>
        <div class="flex items-start gap-1.5 text-ap-down" :class="{ 'border-t border-t-ap-line border-t-solid pt-1.5': decision.reevaluate.length }">
          <el-icon class="mt-0.5 shrink-0">
            <Warning />
          </el-icon>
          <span><b>风险：</b>{{ decision.risks }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.label {
  margin-right: 8px;
  color: var(--ap-muted);
  font-family: var(--ap-font-mono);
  font-size: 12px;
}
.state-strip {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border: 1px solid var(--ap-line);
  background: var(--ap-panel-highest);
  color: var(--ap-muted);
  font-size: 12px;
}
.param {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 4px;
  border-bottom: 1px solid color-mix(in srgb, var(--ap-line) 60%, transparent);
}
.param > span:first-child {
  color: var(--ap-muted);
  font-size: 11px;
}
</style>
