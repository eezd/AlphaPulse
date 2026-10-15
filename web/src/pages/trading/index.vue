<script lang="ts" setup>
import type { Effort, Provider } from "@/apis/settings"
import type { Analysis, OrderRow, SnapshotPosition, TradingEvent, TradingStatus, TradingSymbol } from "@/apis/trading"
import { Clock, Edit, Plus } from "@element-plus/icons-vue"
import { EFFORTS, listProviders } from "@/apis/settings"
import { deleteSymbol, getStatus, getSymbolPlan, isOwnOrder, listAnalyses, listEvents, listSymbols, patchConfig, requestAnalysis, startTrading, stopTrading } from "@/apis/trading"
import { usePersisted } from "@/utils/storage"
import { formatTime } from "@/utils/text"
import AddSymbolDialog from "./components/AddSymbolDialog.vue"
import AnalysisDrawer from "./components/AnalysisDrawer.vue"
import PlanCard from "./components/PlanCard.vue"
import PositionHistoryDrawer from "./components/PositionHistoryDrawer.vue"

const STATUS_REFRESH_MS = 3000
const EVENTS_PAGE = 50
const LEVEL_TYPES = { info: "primary", warn: "warning", error: "danger" } as const

const status = ref<TradingStatus>()
const symbols = ref<TradingSymbol[]>([])
const providers = ref<Provider[]>([])
const selected = usePersisted("trading.symbol", "")
const events = ref<TradingEvent[]>([])
const eventsExhausted = ref(false)
const addVisible = ref(false)
const toggling = ref(false)
const latestAnalysis = ref<Analysis | null>(null)
const livePlan = ref<Analysis | null>(null)
const planOrders = ref<OrderRow[]>([])
const requesting = ref(false)
const drawerVisible = ref(false)
const drawerAnalysisId = ref<number | null>(null)
const historyVisible = ref(false)

const snapshot = computed(() => status.value?.snapshot ?? null)
const account = computed(() => snapshot.value?.account ?? null)
const positions = computed(() => snapshot.value?.positions ?? [])
const current = computed(() => symbols.value.find(s => s.symbol === selected.value))
const engineText = computed(() => snapshot.value ? `在线 · ${status.value!.running ? "正在交易" : "已停止"}` : "离线")
const capitalShort = computed(() => {
  const s = status.value!
  return s.effective_capital && s.capital_usdt && Number(s.effective_capital) < Number(s.capital_usdt)
})
const symbolOrders = computed(() => {
  const s = snapshot.value
  if (!s)
    return []
  return [
    ...s.orders.filter(o => o.symbol === selected.value).map(o => ({
      ...o,
      conditional: false,
      priceText: num(o.price),
      amount: `${usdtValue(o.price, o.quantity)} USDT`
    })),
    ...s.algo_orders.filter(o => o.symbol === selected.value).map(o => ({
      ...o,
      conditional: true,
      priceText: `触发 ${num(o.trigger_price)}`,
      amount: o.close_position ? "全部持仓" : `${usdtValue(o.trigger_price, o.quantity)} USDT`
    }))
  ].sort((a, b) => b.time - a.time)
})

function num(value: string) {
  return String(Number(value))
}

/** 币数量按价格折成 USDT 金额；页面上的仓位、挂单大小都用 USDT 显示 */
function usdtValue(price: string, quantity: string) {
  return (Number(price) * Number(quantity)).toFixed(2)
}

function money(value: string | undefined) {
  return value === undefined ? "-" : Number(value).toFixed(2)
}

function pnlClass(value: string) {
  const n = Number(value)
  return n > 0 ? "text-[var(--el-color-success)]" : n < 0 ? "text-[var(--el-color-danger)]" : ""
}

/** 持仓当前生效的止损、止盈触发价：取交易所上平仓方向的挂单 */
function exitPrices(p: SnapshotPosition) {
  const s = snapshot.value!
  const closeSide = p.side === "LONG" ? "SELL" : "BUY"
  const stops = s.algo_orders.filter(o => o.symbol === p.symbol && o.side === closeSide && o.type.startsWith("STOP")).map(o => Number(o.trigger_price))
  const targets = [
    ...s.orders.filter(o => o.symbol === p.symbol && o.side === closeSide && o.reduce_only && o.type === "LIMIT").map(o => Number(o.price)),
    ...s.algo_orders.filter(o => o.symbol === p.symbol && o.side === closeSide && o.type.startsWith("TAKE_PROFIT")).map(o => Number(o.trigger_price))
  ]
  const mark = Number(p.mark_price)
  const nearest = (prices: number[]) => prices.length ? String(prices.reduce((a, b) => Math.abs(a - mark) < Math.abs(b - mark) ? a : b)) : "无"
  return { stop: nearest(stops), target: nearest(targets) }
}

function positionOf(symbol: string) {
  return positions.value.find(p => p.symbol === symbol)
}

function symbolState(s: TradingSymbol) {
  if (s.analyzing)
    return { label: "分析中", type: "warning" } as const
  if (s.plan_status === "closing")
    return { label: "平仓中", type: "danger" } as const
  if (positionOf(s.symbol))
    return { label: "持仓中", type: "success" } as const
  if (s.plan_status === "pending")
    return { label: "挂单中", type: "primary" } as const
  return { label: "空仓", type: "info" } as const
}

async function refresh() {
  [status.value, symbols.value] = await Promise.all([getStatus(), listSymbols()])
  if (!current.value && symbols.value.length)
    selected.value = symbols.value[0].symbol
  await refreshAnalysis()
}

async function refreshAnalysis() {
  const symbol = selected.value
  if (!symbol)
    return
  const [latest, { plan, orders }] = await Promise.all([listAnalyses({ symbol, limit: 1 }), getSymbolPlan(symbol)])
  if (symbol !== selected.value)
    return
  latestAnalysis.value = latest[0] ?? null
  livePlan.value = plan
  planOrders.value = orders
}

/** 持仓的平仓倒计时：取该币执行中计划的最晚平仓时间 */
function closeCountdown(symbol: string) {
  const deadline = symbols.value.find(s => s.symbol === symbol)?.close_deadline
  if (!deadline)
    return "-"
  const minutes = Math.round((new Date(deadline).getTime() - Date.now()) / 60000)
  return minutes <= 0 ? "已到期" : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

async function analyze() {
  requesting.value = true
  try {
    await requestAnalysis(selected.value)
    ElMessage.success(status.value?.running ? "已排队分析" : "已排队试运行分析：计划只记录，不下单")
    await refresh()
  } finally {
    requesting.value = false
  }
}

function openAnalysis(id: number) {
  drawerAnalysisId.value = id
  drawerVisible.value = true
}

/** 拉取最新一页，与已加载的合并（保留已翻页加载的旧事件） */
async function refreshEvents() {
  const symbol = selected.value
  const latest = await listEvents({ symbol: symbol || undefined, limit: EVENTS_PAGE })
  // 请求期间切换了币种：丢弃旧币种的结果
  if (symbol !== selected.value)
    return
  const newestId = events.value[0]?.id ?? 0
  const fresh = latest.filter(e => e.id > newestId)
  if (!events.value.length)
    eventsExhausted.value = latest.length < EVENTS_PAGE
  events.value = [...fresh, ...events.value]
}

async function loadMoreEvents() {
  const symbol = selected.value
  const older = await listEvents({ symbol: symbol || undefined, before: events.value.at(-1)?.id, limit: EVENTS_PAGE })
  if (symbol !== selected.value)
    return
  eventsExhausted.value = older.length < EVENTS_PAGE
  events.value = [...events.value, ...older]
}

watch(selected, () => {
  events.value = []
  latestAnalysis.value = null
  livePlan.value = null
  planOrders.value = []
  refreshEvents()
  refreshAnalysis()
})

async function toggleRunning() {
  if (status.value!.running) {
    await ElMessageBox.confirm(
      "停止后不再做新的分析，并撤销未成交的限价单。已有持仓保留，止损、止盈、到期平仓等程序规则继续执行。",
      "停止行情交易",
      { type: "warning", confirmButtonText: "停止" }
    )
  } else {
    await ElMessageBox.confirm(
      "开始后，引擎会按触发条件和例行节奏自动分析交易列表里的币，并在币安模拟盘下单。",
      "开始行情交易",
      { type: "info", confirmButtonText: "开始" }
    )
  }
  toggling.value = true
  try {
    status.value = status.value!.running ? await stopTrading() : await startTrading()
    await refreshEvents()
  } finally {
    toggling.value = false
  }
}

async function editCapital() {
  const { value } = await ElMessageBox.prompt(
    `仓位按交易资金计算：单笔风险 1%（置信度低时 0.5%），所有仓位名义价值合计不超过 3 倍（逐仓 3 倍时保证金合计不超过交易资金）。不能超过账户权益 ${money(account.value?.margin_balance)} USDT；账户权益低于它时按权益计算。`,
    "设置交易资金（USDT）",
    {
      inputValue: status.value!.capital_usdt ? num(status.value!.capital_usdt) : "",
      inputPattern: /^(?:[1-9]\d*|0)(?:\.\d{1,2})?$/,
      inputErrorMessage: "请输入正数，最多两位小数",
      confirmButtonText: "保存"
    }
  )
  status.value = await patchConfig({ capital_usdt: value })
  ElMessage.success("已保存")
  await refreshEvents()
}

async function changeProvider(providerId: number | null) {
  status.value = await patchConfig({ provider_id: providerId })
}

async function changeEffort(effort: Effort | null) {
  status.value = await patchConfig({ reasoning_effort: effort })
}

async function remove(s: TradingSymbol) {
  await ElMessageBox.confirm(`把 ${s.symbol} 移出交易列表？历史分析和时间线会保留。`, "删除确认", { type: "warning" })
  await deleteSymbol(s.symbol)
  ElMessage.success("已移出")
  await refresh()
}

async function onAdded(symbol: string) {
  await refresh()
  selected.value = symbol
}

refresh()
refreshEvents()
listProviders().then(list => providers.value = list)
const timer = setInterval(() => {
  refresh()
  refreshEvents()
}, STATUS_REFRESH_MS)
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div v-if="status" class="flex flex-col gap-4">
    <!-- 顶部：引擎状态、启停、账户 -->
    <el-card shadow="never" body-class="flex flex-wrap items-center gap-4">
      <div class="flex flex-col gap-2">
        <div class="flex items-center gap-3 border border-slate-200 rounded-lg border-solid bg-slate-50 px-3 py-2">
          <span class="h-3 w-3 rounded-full" :class="snapshot ? 'bg-emerald-500 pulse' : 'bg-slate-300'" />
          <div>
            <div class="text-xs text-slate-500">
              交易引擎
            </div>
            <div class="text-sm font-semibold" :class="snapshot ? 'text-emerald-700' : 'text-slate-500'">
              {{ engineText }}
            </div>
          </div>
        </div>
        <el-button
          :type="status.running ? 'danger' : 'primary'" :plain="status.running" :loading="toggling" size="small"
          :disabled="!status.running && (!status.configured || !status.capital_usdt)"
          @click="toggleRunning"
        >
          {{ status.running ? "停止行情交易" : "开始行情交易" }}
        </el-button>
      </div>

      <div class="h-12 w-px bg-slate-200" />

      <div class="metric">
        <div class="metric-label">
          交易资金
          <el-icon class="cursor-pointer text-[var(--el-color-primary)]" title="设置交易资金" @click="editCapital">
            <Edit />
          </el-icon>
        </div>
        <div v-if="status.capital_usdt" class="metric-value">
          {{ money(status.capital_usdt) }}<small>USDT</small>
        </div>
        <div v-else class="metric-value text-rose-600!">
          未设置
        </div>
        <div v-if="capitalShort" class="text-xs text-amber-600">
          权益不足，按 {{ money(status.effective_capital!) }}
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">
          账户权益
        </div>
        <div class="metric-value">
          {{ money(account?.margin_balance) }}<small>USDT</small>
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">
          可用余额
        </div>
        <div class="metric-value">
          {{ money(account?.available_balance) }}<small>USDT</small>
        </div>
      </div>
      <div
        class="metric"
        :class="{ 'bg-emerald-50! border-emerald-200!': Number(account?.unrealized_pnl) > 0, 'bg-rose-50! border-rose-200!': Number(account?.unrealized_pnl) < 0 }"
      >
        <div class="metric-label">
          未实现盈亏
        </div>
        <div class="metric-value" :class="pnlClass(account?.unrealized_pnl ?? '0')">
          {{ money(account?.unrealized_pnl) }}<small>USDT</small>
        </div>
      </div>

      <div class="h-12 w-px bg-slate-200" />

      <div class="w-56">
        <div class="flex items-center justify-between text-xs text-slate-500">
          今日分析
          <span class="num text-sm text-slate-900 font-semibold">{{ status.today.analyses }} / {{ status.today.limit }}</span>
        </div>
        <el-progress :percentage="Math.min(100, status.today.analyses / status.today.limit * 100)" :show-text="false" :stroke-width="4" class="my-1" />
        <div class="num text-xs text-slate-500">
          token 入 {{ status.today.input_tokens.toLocaleString() }} / 出 {{ status.today.output_tokens.toLocaleString() }}
        </div>
      </div>

      <div class="flex-1" />

      <div class="flex flex-col gap-2 text-xs text-slate-500">
        <div class="flex items-center gap-2">
          <span class="w-14">模型</span>
          <el-select
            :model-value="status.provider_id" size="small" class="w-44" clearable :value-on-clear="null" placeholder="跟随默认配置"
            @update:model-value="changeProvider"
          >
            <el-option v-for="p in providers" :key="p.id" :value="p.id" :label="`${p.name}（${p.model}）`" />
          </el-select>
        </div>
        <div class="flex items-center gap-2">
          <span class="w-14">思考强度</span>
          <el-select
            :model-value="status.reasoning_effort" size="small" class="w-44" clearable :value-on-clear="null" placeholder="跟随配置"
            @update:model-value="changeEffort"
          >
            <el-option v-for="e in EFFORTS" :key="e" :value="e" :label="e" />
          </el-select>
        </div>
      </div>
    </el-card>

    <el-alert v-if="!status.configured" type="warning" :closable="false" show-icon>
      还没有填写币安模拟盘 key，请先到「环境变量」页填写。
    </el-alert>
    <el-alert v-else-if="!status.capital_usdt" type="warning" :closable="false" show-icon>
      还没有设置交易资金。引擎只按交易资金计算仓位，不会动用账户的全部余额；设置后才能开始行情交易。
    </el-alert>
    <el-alert v-else-if="snapshot?.error" type="error" :closable="false" show-icon>
      同步模拟盘失败：{{ snapshot.error }}{{ snapshot.synced_at ? `（以下为 ${formatTime(snapshot.synced_at, "HH:mm:ss")} 的数据）` : "" }}
    </el-alert>

    <!-- 持仓 -->
    <el-card shadow="never" body-class="p-0">
      <template #header>
        <div class="flex items-center justify-between">
          持仓（{{ positions.length }}）
          <el-button link type="primary" :icon="Clock" @click="historyVisible = true">
            历史仓位
          </el-button>
        </div>
      </template>
      <el-table :data="positions" empty-text="没有持仓" cell-class-name="num">
        <el-table-column label="币种" min-width="120">
          <template #default="{ row }">
            <div class="text-slate-900 font-semibold">
              {{ row.symbol }}
            </div>
            <div class="text-xs text-slate-400 font-sans">
              永续
            </div>
          </template>
        </el-table-column>
        <el-table-column label="方向" width="80">
          <template #default="{ row }">
            <el-tag :type="row.side === 'LONG' ? 'success' : 'danger'" size="small">
              {{ row.side === "LONG" ? "↑ 多" : "↓ 空" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="仓位价值（USDT）" min-width="120">
          <template #default="{ row }">
            <span :title="`数量 ${num(row.amount)}`">{{ Math.abs(Number(row.notional)).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="开仓价" min-width="100">
          <template #default="{ row }">
            {{ num(row.entry_price) }}
          </template>
        </el-table-column>
        <el-table-column label="保本价" min-width="100">
          <template #default="{ row }">
            <span class="text-slate-500">{{ Number(row.break_even_price).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="标记价" min-width="100">
          <template #default="{ row }">
            <span class="text-[var(--el-color-primary)]">{{ Number(row.mark_price).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="未实现盈亏" min-width="100">
          <template #default="{ row }">
            <span :class="pnlClass(row.unrealized_pnl)">{{ money(row.unrealized_pnl) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="强平价" min-width="100">
          <template #default="{ row }">
            <span class="text-amber-600">{{ Number(row.liquidation_price) ? Number(row.liquidation_price).toFixed(2) : "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="杠杆" width="100">
          <template #default="{ row }">
            <el-tag type="info" size="small">
              {{ row.leverage }}x {{ row.margin_type === "ISOLATED" ? "逐仓" : "全仓" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="止损" min-width="100">
          <template #default="{ row }">
            <span class="text-rose-600">{{ exitPrices(row as SnapshotPosition).stop }}</span>
          </template>
        </el-table-column>
        <el-table-column label="止盈" min-width="100">
          <template #default="{ row }">
            <span class="text-emerald-600">{{ exitPrices(row as SnapshotPosition).target }}</span>
          </template>
        </el-table-column>
        <el-table-column label="平仓倒计时" width="110">
          <template #default="{ row }">
            <el-tag type="warning" size="small">
              {{ closeCountdown(row.symbol) }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="grid grid-cols-[260px_minmax(0,1fr)_340px] items-start gap-4">
      <!-- 左：交易币种 -->
      <el-card shadow="never" body-class="flex flex-col gap-2">
        <template #header>
          <div class="flex items-center justify-between">
            交易币种
            <el-button link type="primary" :icon="Plus" @click="addVisible = true">
              新增币种
            </el-button>
          </div>
        </template>
        <div
          v-for="s in symbols" :key="s.symbol"
          class="relative cursor-pointer overflow-hidden border rounded-lg border-solid py-2 pl-4 pr-3 transition-colors"
          :class="s.symbol === selected ? 'border-[var(--el-color-primary)] bg-sky-50/60' : 'border-slate-200 hover:border-slate-300'"
          @click="selected = s.symbol"
        >
          <span class="absolute inset-y-0 left-0 w-1" :style="{ background: `var(--el-color-${symbolState(s).type})` }" />
          <div class="flex items-center justify-between">
            <span class="num text-slate-900 font-semibold">{{ s.symbol }}</span>
            <el-tag :type="symbolState(s).type" size="small">
              {{ symbolState(s).label }}
            </el-tag>
          </div>
          <div class="mt-1 flex items-center justify-between text-xs text-slate-500">
            <span v-if="positionOf(s.symbol)" class="num">
              {{ positionOf(s.symbol)!.leverage }}x {{ positionOf(s.symbol)!.side === "LONG" ? "多" : "空" }} · {{ Math.abs(Number(positionOf(s.symbol)!.notional)).toFixed(2) }} USDT
            </span>
            <span v-else />
            <el-link type="danger" underline="never" class="text-xs!" @click.stop="remove(s)">
              删除
            </el-link>
          </div>
        </div>
        <el-empty v-if="!symbols.length" description="还没有交易币种" :image-size="60" />
      </el-card>

      <!-- 中：当前计划 -->
      <PlanCard
        v-if="current" :symbol="current" :analysis="livePlan ?? latestAnalysis" :latest="latestAnalysis" :orders="planOrders"
        :requesting="requesting"
        @analyze="analyze" @detail="openAnalysis"
      />
      <el-card v-else shadow="never">
        <el-empty description="先在左侧新增交易币种" :image-size="60" />
      </el-card>

      <!-- 右：挂单、时间线 -->
      <div class="flex flex-col gap-4">
        <el-card v-if="current" shadow="never" body-class="flex flex-col gap-2">
          <template #header>
            当前挂单（{{ symbolOrders.length }}）
          </template>
          <div
            v-for="o in symbolOrders" :key="`${o.client_id}-${o.time}`"
            class="border border-slate-200 rounded-lg border-solid bg-slate-50/60 px-3 py-2 text-xs"
          >
            <div class="flex items-center justify-between">
              <span class="text-sm font-semibold" :class="o.side === 'BUY' ? 'text-emerald-700' : 'text-rose-700'">
                {{ o.side === "BUY" ? "买" : "卖" }} · {{ o.type }}
              </span>
              <span class="num text-slate-400">{{ formatTime(new Date(o.time).toISOString(), "MM-DD HH:mm:ss") }}</span>
            </div>
            <div class="num mt-1 flex justify-between text-sm text-slate-900">
              <span :title="o.amount === '全部持仓' ? '' : `数量 ${num(o.quantity)}`">{{ o.amount }}</span>
              <span>@ {{ o.priceText }}</span>
            </div>
            <div class="mt-1 flex items-center gap-1 text-slate-500">
              <el-tag v-if="o.conditional" size="small">
                条件单
              </el-tag>
              <el-tag v-if="o.reduce_only" size="small" type="info">
                只减仓
              </el-tag>
              <span class="ml-auto">来源：{{ isOwnOrder(o.client_id) ? "本程序" : "手动" }}</span>
            </div>
          </div>
          <el-empty v-if="!symbolOrders.length" description="没有挂单" :image-size="48" />
        </el-card>

        <el-card shadow="never">
          <template #header>
            时间线
          </template>
          <div v-if="events.length" class="max-h-180 overflow-auto pr-1">
            <div v-for="e in events" :key="e.id" class="timeline-item relative pb-4 pl-5">
              <span class="absolute left-0 top-1 h-[11px] w-[11px] border-2 border-white rounded-full border-solid" :style="{ background: `var(--el-color-${LEVEL_TYPES[e.level]})` }" />
              <div class="flex items-center gap-2 text-xs">
                <span class="num text-slate-500">{{ formatTime(e.created_at) }}</span>
                <el-tag size="small" :type="LEVEL_TYPES[e.level]">
                  {{ e.level.toUpperCase() }}
                </el-tag>
                <el-tag size="small" type="info">
                  {{ e.symbol ?? "账户" }}
                </el-tag>
              </div>
              <div class="mt-1 text-sm" :class="e.level === 'error' ? 'text-rose-600' : 'text-slate-700'">
                {{ e.message }}
                <el-link v-if="e.analysis_id && e.kind !== 'analysis_started'" type="primary" underline="never" class="ml-1" @click="openAnalysis(e.analysis_id)">
                  查看分析
                </el-link>
              </div>
            </div>
          </div>
          <el-empty v-else description="还没有事件" :image-size="60" />
          <el-button v-if="events.length && !eventsExhausted" link type="primary" @click="loadMoreEvents">
            加载更早的事件
          </el-button>
        </el-card>
      </div>
    </div>

    <AddSymbolDialog v-model="addVisible" @added="onAdded" />
    <PositionHistoryDrawer v-model="historyVisible" @detail="openAnalysis" />
    <AnalysisDrawer v-model="drawerVisible" :analysis-id="drawerAnalysisId" />
  </div>
</template>

<style scoped>
.metric {
  min-width: 120px;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-bg-color);
}
.metric-label {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.metric-value {
  margin-top: 2px;
  color: var(--el-text-color-primary);
  font-family: var(--ap-font-mono);
  font-size: 18px;
  font-weight: 600;
}
.metric-value small {
  margin-left: 4px;
  color: var(--el-text-color-secondary);
  font-size: 11px;
  font-weight: 400;
}
/* 竖线连到下一条事件 */
.timeline-item::before {
  content: "";
  position: absolute;
  top: 12px;
  bottom: 0;
  left: 5px;
  width: 1px;
  background: var(--el-border-color);
}
.timeline-item:last-child::before {
  display: none;
}
.pulse {
  box-shadow: 0 0 0 0 rgb(16 185 129 / 50%);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  70% {
    box-shadow: 0 0 0 6px rgb(16 185 129 / 0%);
  }
}
</style>
