<script lang="ts" setup>
import type { Analysis, OrderRow, PlanCondition, TradingSymbol } from "@/apis/trading"
import { Document, Edit, Lightning, SwitchButton, VideoPlay } from "@element-plus/icons-vue"
import { CLOSE_REASON_LABELS, PLAN_STATUS_LABELS, TAKE_PROFIT_STEPS } from "@/apis/trading"
import { formatTime } from "@/utils/text"

/** analysis：执行中的计划，没有时为最近一次分析；latest：最近一次分析（与 analysis 不同时另行提示） */
const props = defineProps<{ symbol: TradingSymbol, analysis: Analysis | null, latest: Analysis | null, orders: OrderRow[], requesting: boolean, toggling: boolean }>()
const emit = defineEmits<{ analyze: [], detail: [id: number], toggle: [], capital: [] }>()

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
const duration = computed(() => props.analysis?.finished_at ? Math.round((new Date(props.analysis.finished_at).getTime() - new Date(props.analysis.started_at).getTime()) / 1000) : null)

/** 已成交金额（USDT），按每笔的成交均价折算；有成交的订单一定有均价 */
function filled(purpose: "entry" | "tp", leg: number) {
  const total = props.orders
    .filter(o => o.purpose === purpose && o.leg === leg && Number(o.filled_qty) > 0)
    .reduce((sum, o) => sum + Number(o.filled_qty) * Number(o.avg_price!), 0)
  return total ? `${total.toFixed(2)} USDT` : "-"
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
      filled: filled("tp", leg),
      active: ACTIVE_ORDER_STATUSES.includes(props.orders.filter(o => o.purpose === "tp" && o.leg === leg).at(-1)?.status ?? "")
    }))
  : [])
</script>

<template>
  <el-card shadow="never" body-class="flex flex-col gap-3 text-sm">
    <template #header>
      <div class="flex items-center gap-3">
        <span class="num h-9 w-9 flex-center border border-sky-200 rounded-lg border-solid bg-sky-50 text-base text-[var(--el-color-primary)]">
          {{ symbol.symbol.slice(0, 1) }}
        </span>
        <div class="min-w-0">
          <div class="flex items-center gap-2 text-base text-slate-900">
            <span class="num">{{ symbol.symbol }}</span>交易计划
            <el-tag v-if="analysis?.plan_status" type="success" size="small">
              {{ PLAN_STATUS_LABELS[analysis.plan_status] }}
            </el-tag>
          </div>
          <div class="truncate text-xs text-slate-500 font-normal" title="短线做波动（区间内挂限价单，最长持仓 4 小时）">
            短线做波动（区间内挂限价单，最长持仓 4 小时）
          </div>
        </div>
        <div class="flex-1" />
        <el-button
          :type="symbol.running ? 'danger' : 'success'" plain :icon="symbol.running ? SwitchButton : VideoPlay" :loading="toggling"
          :disabled="!symbol.running && !symbol.capital_usdt" :title="!symbol.running && !symbol.capital_usdt ? '先设置交易资金' : ''"
          @click="emit('toggle')"
        >
          {{ symbol.running ? "停止交易" : "开始交易" }}
        </el-button>
        <el-button v-if="analysis && analysis.status !== 'running'" :icon="Document" @click="emit('detail', analysis.id)">
          查看完整分析
        </el-button>
        <el-button type="primary" :icon="Lightning" :loading="busy" @click="emit('analyze')">
          {{ busy ? "分析中" : "立即分析" }}
        </el-button>
      </div>
    </template>

    <div class="flex flex-wrap items-center gap-x-6 gap-y-1 border border-slate-200 rounded-lg border-solid bg-slate-50 px-3 py-2 text-xs text-slate-500">
      <span class="flex items-center gap-1">
        交易资金
        <b v-if="symbol.capital_usdt" class="num text-sm text-slate-900">{{ Number(symbol.capital_usdt).toFixed(2) }} USDT</b>
        <b v-else class="text-sm text-rose-600">未设置</b>
        <el-icon class="cursor-pointer text-[var(--el-color-primary)]" title="设置交易资金" @click="emit('capital')">
          <Edit />
        </el-icon>
      </span>
      <span v-if="symbol.effective_capital && Number(symbol.effective_capital) < Number(symbol.capital_usdt)" class="text-amber-600">
        权益不足，按 {{ Number(symbol.effective_capital).toFixed(2) }} 计算
      </span>
      <span v-if="symbol.capital_usdt">名义仓位上限 <b class="num text-slate-900">{{ (Number(symbol.effective_capital ?? symbol.capital_usdt) * 3).toFixed(2) }} USDT</b></span>
      <span class="flex items-center gap-1">
        <span class="h-2 w-2 rounded-full" :class="symbol.running ? 'bg-emerald-500' : 'bg-slate-300'" />
        <span :class="symbol.running ? 'text-emerald-700' : ''">{{ symbol.running ? "交易中" : "已停止" }}</span>
        <span v-if="symbol.running_changed_at" class="num">（{{ formatTime(symbol.running_changed_at, "MM-DD HH:mm") }} 起）</span>
      </span>
    </div>

    <div v-if="!analysis" class="border border-slate-200 rounded-lg border-solid bg-slate-50 px-3 py-2 text-slate-500">
      还没有分析。该币停止交易时点「立即分析」为试运行：计划只记录，不下单。
    </div>
    <div v-else-if="analysis.status === 'running'" class="flex items-center gap-2 border border-amber-200 rounded-lg border-solid bg-amber-50 px-3 py-2 text-amber-700">
      <span class="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
      模型分析中，已用 {{ elapsed }} 秒（通常 1–3 分钟）…
    </div>
    <el-alert v-else-if="analysis.status === 'failed'" type="error" :closable="false" show-icon>
      {{ formatTime(analysis.started_at) }} 分析失败：{{ analysis.error }}
    </el-alert>

    <template v-else-if="decision">
      <!-- 分析概要与结论 -->
      <div class="flex flex-col gap-2 border border-sky-100 rounded-lg border-solid bg-sky-50/50 p-3">
        <div class="flex flex-wrap items-center gap-2">
          <span class="flex items-center gap-1 text-emerald-700 font-semibold">
            <span class="h-2 w-2 rounded-full bg-emerald-500" />
            分析完成<template v-if="duration !== null">（耗时 {{ duration }}s）</template>
          </span>
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
          <span class="num ml-auto text-xs text-slate-500">{{ formatTime(analysis.started_at) }}</span>
        </div>
        <div class="border-l-4 border-l-[var(--el-color-primary)] rounded-md border-l-solid bg-white px-3 py-2 text-slate-900 leading-relaxed">
          <span class="mr-2 text-[var(--el-color-primary)] font-semibold">结论</span>{{ decision.summary }}
        </div>
        <div class="num text-xs text-slate-500">
          触发：{{ analysis.reasons.join("；") }} · {{ analysis.model }}{{ analysis.reasoning_effort ? ` / ${analysis.reasoning_effort}` : "" }}
          · token {{ analysis.usage?.input_tokens.toLocaleString() }} / {{ analysis.usage?.output_tokens.toLocaleString() }}
        </div>
      </div>
      <div v-if="latest && latest.id !== analysis.id" class="text-xs text-slate-500">
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

      <!-- 时间 -->
      <div v-if="analysis.valid_until || analysis.close_deadline" class="grid grid-cols-3 gap-2">
        <div class="tile">
          <div class="label">
            限价单有效至
          </div>
          <template v-if="analysis.plan_status === 'pending' && analysis.valid_until">
            <div class="num text-slate-900">
              {{ formatTime(analysis.valid_until, "MM-DD HH:mm") }}
            </div>
            <div class="text-xs text-[var(--el-color-primary)]">
              剩 {{ countdown(analysis.valid_until) }}
            </div>
          </template>
          <div v-else class="text-slate-400">
            -
          </div>
        </div>
        <div class="tile">
          <div class="label">
            入场
          </div>
          <div class="num text-slate-900">
            {{ formatTime(analysis.first_fill_at, "MM-DD HH:mm") }}
          </div>
        </div>
        <div class="tile">
          <div class="label">
            最晚平仓
          </div>
          <template v-if="analysis.close_deadline">
            <div class="num text-slate-900">
              {{ formatTime(analysis.close_deadline, "MM-DD HH:mm") }}
            </div>
            <div class="text-xs text-amber-600">
              剩 {{ countdown(analysis.close_deadline) }}
            </div>
          </template>
          <div v-else class="text-slate-400">
            -
          </div>
        </div>
      </div>
      <div
        v-if="analysis.result" class="border rounded-lg border-solid px-3 py-2"
        :class="Number(analysis.result.net) >= 0 ? 'border-emerald-200 bg-emerald-50' : 'border-rose-200 bg-rose-50'"
      >
        <span class="label">结果</span>{{ CLOSE_REASON_LABELS[analysis.close_reason ?? ""] ?? analysis.close_reason }}，净盈亏
        <b class="num" :class="Number(analysis.result.net) >= 0 ? 'text-emerald-700' : 'text-rose-700'">{{ analysis.result.net }}</b> USDT（手续费 {{ analysis.result.commission }}）
      </div>
      <div>
        <div class="label mb-1">
          为什么
        </div>
        <div class="text-slate-700 leading-relaxed">
          {{ decision.reasons }}
        </div>
      </div>

      <!-- 价格与风险 -->
      <template v-if="decision.action === 'plan' && decision.setup && sizing?.entry">
        <div class="flex flex-col gap-2 border border-slate-200 rounded-lg border-solid p-3">
          <div class="flex flex-wrap items-center gap-2">
            <span class="label">{{ long ? "支撑区" : "压力区" }}</span>
            <span
              class="num border rounded border-solid px-2 py-0.5"
              :class="long ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-rose-200 bg-rose-50 text-rose-700'"
            >{{ sizing.setup.zone_low }} – {{ sizing.setup.zone_high }}</span>
            <span class="text-xs text-slate-500">{{ decision.setup.basis }}</span>
          </div>
          <div class="kv">
            <span class="label">限价{{ long ? "做多" : "做空" }}</span>
            <span><span class="num text-slate-900">{{ sizing.entry.price }}</span>
              <span class="ml-2 text-xs text-slate-500">已成交 {{ filled("entry", 0) }}{{ entryRow?.avg_price ? `，均价 ${entryRow.avg_price}` : "" }}</span></span>
          </div>
          <div class="kv">
            <span class="label">下单金额</span>
            <span><span class="num text-slate-900" :title="`数量 ${sizing.entry.quantity}`">{{ sizing.entry.notional }} USDT</span>
              <span class="ml-2 text-xs text-slate-500">{{ sizing.entry.leverage }} 倍交易资金</span></span>
          </div>
          <div class="kv">
            <span class="label">止损</span>
            <span><span class="num text-rose-600 font-semibold">{{ currentStop ?? sizing.entry.stop }}</span>
              <span class="ml-2 text-xs text-slate-500">初始 {{ sizing.entry.stop }}，距挂单价 {{ sizing.entry.stop_pct }}%；区间远端外 0.5×ATR(5m)、距挂单价至少 1×ATR(5m)，止盈 1 后移到保本价</span></span>
          </div>
          <div class="kv">
            <span class="label">止损亏损</span>
            <span><span class="num text-rose-600">{{ sizing.entry.risk_at_stop }} USDT</span>
              <span class="ml-2 text-xs text-slate-500">交易资金的 {{ sizing.entry.risk_pct }}%{{ sizing.entry.capped_by_leverage ? "，受 3 倍上限限制" : "" }}</span></span>
          </div>
        </div>
        <div v-if="ladder.length">
          <div class="label mb-1">
            分批止盈
          </div>
          <el-table :data="ladder" size="small" cell-class-name="num" class="border border-slate-200 rounded-lg border-solid">
            <el-table-column label="档位" min-width="70">
              <template #default="{ row }">
                <span class="text-[var(--el-color-primary)]">止盈 {{ row.leg + 1 }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="move" label="距成交均价" min-width="90" />
            <el-table-column prop="ratio" label="平仓比例" min-width="80" />
            <el-table-column prop="price" label="价格" min-width="100" />
            <el-table-column label="状态" min-width="120" align="right">
              <template #default="{ row }">
                <el-tag v-if="row.filled !== '-'" type="success" size="small">
                  已成交 {{ row.filled }}
                </el-tag>
                <el-tag v-else-if="row.active" size="small">
                  挂单中
                </el-tag>
                <span v-else class="text-slate-400">-</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </template>

      <div v-if="decision.reevaluate.length" class="border border-amber-200 rounded-lg border-solid bg-amber-50 px-3 py-2">
        <div class="mb-1 text-amber-800 font-semibold">
          重新判断
        </div>
        <div v-for="(c, i) in decision.reevaluate" :key="i" class="text-amber-900/80">
          · {{ condition(c) }}：{{ c.basis }}
        </div>
      </div>
      <div class="border border-rose-200 rounded-lg border-solid bg-rose-50 px-3 py-2">
        <div class="mb-1 text-rose-700 font-semibold">
          风险
        </div>
        <div class="text-rose-900/80">
          {{ decision.risks }}
        </div>
      </div>
    </template>
  </el-card>
</template>

<style scoped>
.label {
  margin-right: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.tile {
  padding: 8px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-fill-color-light);
}
.kv {
  display: flex;
  align-items: baseline;
}
.kv > .label {
  flex-shrink: 0;
  width: 64px;
}
</style>
