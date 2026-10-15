<script lang="ts" setup>
import type { Effort, Provider } from "@/apis/settings"
import type { Analysis, OrderRow, SnapshotPosition, TradingEvent, TradingStatus, TradingSymbol } from "@/apis/trading"
import { DataAnalysis } from "@element-plus/icons-vue"
import { EFFORTS, listProviders } from "@/apis/settings"
import { deleteSymbol, getStatus, getSymbolPlan, isOwnOrder, listAnalyses, listEvents, listSymbols, patchConfig, requestAnalysis, startTrading, stopTrading } from "@/apis/trading"
import { usePersisted } from "@/utils/storage"
import { formatTime } from "@/utils/text"
import AddSymbolDialog from "./components/AddSymbolDialog.vue"
import AnalysisDrawer from "./components/AnalysisDrawer.vue"
import PlanCard from "./components/PlanCard.vue"

const STATUS_REFRESH_MS = 3000
const EVENTS_PAGE = 50
const LEVEL_DOTS = { info: "bg-ap-faint", warn: "bg-ap-warn", error: "bg-ap-down" } as const

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

const snapshot = computed(() => status.value?.snapshot ?? null)
const account = computed(() => snapshot.value?.account ?? null)
const positions = computed(() => snapshot.value?.positions ?? [])
const current = computed(() => symbols.value.find(s => s.symbol === selected.value))
const symbolOrders = computed(() => {
  const s = snapshot.value
  if (!s)
    return []
  return [
    ...s.orders.filter(o => o.symbol === selected.value).map(o => ({
      ...o,
      conditional: false,
      priceText: num(o.price)
    })),
    ...s.algo_orders.filter(o => o.symbol === selected.value).map(o => ({
      ...o,
      conditional: true,
      priceText: `触发 ${num(o.trigger_price)}`,
      filled: "0",
      quantity: o.close_position ? "全部持仓" : o.quantity
    }))
  ].sort((a, b) => b.time - a.time)
})

/** 价格、数量：千分位，保留原有小数 */
function num(value: string) {
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 8 })
}

function money(value: string | undefined) {
  return value === undefined ? "-" : Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function signedMoney(value: string | undefined) {
  return value !== undefined && Number(value) > 0 ? `+${money(value)}` : money(value)
}

function pnlClass(value: string) {
  const n = Number(value)
  return n > 0 ? "text-ap-up" : n < 0 ? "text-ap-down" : ""
}

const winRate = computed(() => {
  const { trades, wins } = status.value!.today
  return trades ? `${(wins / trades * 100).toFixed(1)}%` : "-"
})

/** 交易币种列表第二行：该币的持仓 */
function positionLine(symbol: string) {
  const p = positions.value.find(x => x.symbol === symbol)
  return p ? { text: `${p.leverage}x ${p.side === "LONG" ? "多" : "空"} · ${num(p.amount)}`, long: p.side === "LONG" } : null
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

function symbolState(s: TradingSymbol) {
  if (s.analyzing)
    return { label: "分析中", type: "warning" } as const
  if (s.plan_status === "closing")
    return { label: "平仓中", type: "danger" } as const
  if (positions.value.some(p => p.symbol === s.symbol))
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
      inputValue: status.value!.capital_usdt ? String(Number(status.value!.capital_usdt)) : "",
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
  <div v-if="status" class="h-full flex flex-col gap-2">
    <!-- 状态栏：引擎、启停、账户与今日统计、模型 -->
    <div class="flex flex-col gap-2 border border-ap-line border-solid bg-ap-panel px-3 py-2.5">
      <div class="flex flex-wrap items-center gap-2">
        <el-tag :type="snapshot ? 'success' : 'info'">
          <i class="mr-1.5 inline-block h-1.5 w-1.5 bg-current" :class="{ 'animate-pulse': snapshot }" />{{ snapshot ? "引擎在线" : "引擎离线" }}
        </el-tag>
        <el-tag :type="status.running ? 'success' : 'info'">
          <i class="mr-1.5 inline-block h-1.5 w-1.5 bg-current" />{{ status.running ? "交易中" : "已停止" }}
        </el-tag>
        <el-button
          size="small" :type="status.running ? 'danger' : 'primary'" :loading="toggling"
          :disabled="!status.running && (!status.configured || !status.capital_usdt)"
          @click="toggleRunning"
        >
          {{ status.running ? "停止" : "开始行情交易" }}
        </el-button>
        <div class="flex-1" />
        <span class="metric mr-2">
          <span class="key">今日分析</span>{{ status.today.analyses }} / {{ status.today.limit }}
          <span class="key ml-1.5">token</span>{{ status.today.input_tokens.toLocaleString() }} / {{ status.today.output_tokens.toLocaleString() }}
        </span>
        <el-select
          :model-value="status.provider_id" class="w-64" clearable :value-on-clear="null" placeholder="跟随默认配置"
          @update:model-value="changeProvider"
        >
          <template #prefix>
            <span class="text-[11px] text-ap-muted">模型:</span>
          </template>
          <el-option v-for="p in providers" :key="p.id" :value="p.id" :label="`${p.name}（${p.model}）`" />
        </el-select>
        <el-select
          :model-value="status.reasoning_effort" class="w-36" clearable :value-on-clear="null" placeholder="跟随配置"
          @update:model-value="changeEffort"
        >
          <template #prefix>
            <span class="text-[11px] text-ap-muted">思考:</span>
          </template>
          <el-option v-for="e in EFFORTS" :key="e" :value="e" :label="e" />
        </el-select>
      </div>
      <div class="flex flex-wrap items-center gap-y-1">
        <span class="metric">
          <span class="key">交易资金</span>
          <template v-if="status.capital_usdt">
            <b>{{ money(status.capital_usdt) }}</b><span class="unit">USDT</span>
            <span v-if="status.effective_capital && Number(status.effective_capital) < Number(status.capital_usdt)" class="ml-1 text-ap-warn">
              （权益不足，按 {{ money(status.effective_capital) }}）
            </span>
          </template>
          <b v-else class="text-ap-down">未设置</b>
          <el-link type="primary" class="ml-1.5 text-[11px]!" @click="editCapital">设置</el-link>
        </span>
        <span class="metric"><span class="key">账户权益</span><b>{{ money(account?.margin_balance) }}</b><span class="unit">USDT</span></span>
        <span class="metric"><span class="key">可用</span><b>{{ money(account?.available_balance) }}</b><span class="unit">USDT</span></span>
        <span class="metric">
          <span class="key">未实现盈亏</span><b :class="pnlClass(account?.unrealized_pnl ?? '0')">{{ signedMoney(account?.unrealized_pnl) }}</b>
        </span>
        <span class="metric">
          <span class="key">今日收益</span><b :class="pnlClass(status.today.net_pnl)">{{ signedMoney(status.today.net_pnl) }}</b><span class="unit">USDT</span>
        </span>
        <span class="metric"><span class="key">今日交易</span><b>{{ status.today.trades }}</b><span class="unit">笔</span></span>
        <span class="metric"><span class="key">胜率</span><b class="text-ap-brand">{{ winRate }}</b></span>
      </div>
    </div>

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
    <el-card shadow="never" body-class="p-0!">
      <template #header>
        <i class="inline-block h-1.5 w-1.5" :class="positions.length ? 'bg-ap-up' : 'bg-ap-faint'" />
        <span>持仓 ({{ positions.length }})</span>
      </template>
      <el-table :data="positions" size="small" empty-text="没有持仓">
        <el-table-column label="币种" min-width="130">
          <template #default="{ row }">
            <span class="text-ap-brand font-bold">{{ row.symbol }}</span>
            <el-tag size="small" type="info" class="ml-1.5">
              永续
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="方向" width="70">
          <template #default="{ row }">
            <el-tag :type="row.side === 'LONG' ? 'success' : 'danger'" size="small">
              {{ row.side === "LONG" ? "多" : "空" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="数量" min-width="90" align="right">
          <template #default="{ row }">
            {{ num(row.amount) }}
          </template>
        </el-table-column>
        <el-table-column label="开仓价" min-width="100" align="right">
          <template #default="{ row }">
            {{ num(row.entry_price) }}
          </template>
        </el-table-column>
        <el-table-column label="保本价" min-width="100" align="right">
          <template #default="{ row }">
            <span class="text-ap-muted">{{ money(row.break_even_price) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="标记价" min-width="100" align="right">
          <template #default="{ row }">
            <b>{{ money(row.mark_price) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="未实现盈亏" min-width="100" align="right">
          <template #default="{ row }">
            <b :class="pnlClass(row.unrealized_pnl)">{{ signedMoney(row.unrealized_pnl) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="强平价" min-width="100" align="right">
          <template #default="{ row }">
            {{ Number(row.liquidation_price) ? money(row.liquidation_price) : "-" }}
          </template>
        </el-table-column>
        <el-table-column label="杠杆" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="info">
              {{ row.leverage }}x {{ row.margin_type === "ISOLATED" ? "逐仓" : "全仓" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="止损 / 止盈" min-width="160" align="center">
          <template #default="{ row }">
            <span class="text-ap-down">{{ exitPrices(row as SnapshotPosition).stop }}</span>
            /
            <span class="text-ap-up">{{ exitPrices(row as SnapshotPosition).target }}</span>
          </template>
        </el-table-column>
        <el-table-column label="平仓倒计时" width="100" align="center">
          <template #default="{ row }">
            <b class="text-ap-brand">{{ closeCountdown(row.symbol) }}</b>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="min-h-0 flex flex-1 gap-2">
      <!-- 交易币种 -->
      <el-card shadow="never" class="w-60 flex shrink-0 flex-col" body-class="p-0! min-h-0 flex-1">
        <template #header>
          <div class="w-full flex items-center justify-between">
            <span>交易币种 ({{ symbols.length }})</span>
            <el-button size="small" type="primary" @click="addVisible = true">
              新增
            </el-button>
          </div>
        </template>
        <el-scrollbar>
          <div
            v-for="s in symbols" :key="s.symbol" class="symbol-item" :class="{ active: s.symbol === selected }"
            @click="selected = s.symbol"
          >
            <div class="flex items-center justify-between">
              <span class="text-sm font-bold">{{ s.symbol }}</span>
              <el-tag :type="symbolState(s).type" size="small">
                {{ symbolState(s).label }}
              </el-tag>
            </div>
            <div class="mt-1 flex items-center justify-between text-[11px]">
              <span v-if="positionLine(s.symbol)" class="font-semibold" :class="positionLine(s.symbol)!.long ? 'text-ap-up' : 'text-ap-down'">
                {{ positionLine(s.symbol)!.text }}
              </span>
              <span v-else />
              <el-link type="danger" class="text-[11px]!" @click.stop="remove(s)">
                删除
              </el-link>
            </div>
          </div>
          <el-empty v-if="!symbols.length" description="还没有交易币种" :image-size="60" />
        </el-scrollbar>
      </el-card>

      <!-- 当前币种：计划、挂单、时间线 -->
      <el-card shadow="never" class="min-w-0 flex flex-1 flex-col" body-class="p-0! min-h-0 flex-1">
        <template #header>
          <el-icon class="text-ap-brand">
            <DataAnalysis />
          </el-icon>
          <span>{{ current ? `${current.symbol} · 交易计划` : "交易计划" }}</span>
        </template>
        <el-scrollbar>
          <div class="flex flex-col gap-3 p-3">
            <PlanCard
              v-if="current" :symbol="current" :analysis="livePlan ?? latestAnalysis" :latest="latestAnalysis" :orders="planOrders"
              :requesting="requesting"
              @analyze="analyze" @detail="openAnalysis"
            />

            <el-card v-if="current" shadow="never" class="sub-card" body-class="p-0!">
              <template #header>
                当前挂单 ({{ symbolOrders.length }})
              </template>
              <el-table :data="symbolOrders" size="small" empty-text="没有挂单">
                <el-table-column label="类型" min-width="150">
                  <template #default="{ row }">
                    {{ row.type }}
                    <el-tag v-if="row.conditional" size="small" type="info" class="ml-1">
                      条件单
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="方向" width="110">
                  <template #default="{ row }">
                    <b :class="row.side === 'BUY' ? 'text-ap-up' : 'text-ap-down'">{{ row.side === "BUY" ? "买" : "卖" }}</b>{{ row.reduce_only ? "（只减仓）" : "" }}
                  </template>
                </el-table-column>
                <el-table-column prop="priceText" label="价格 / 触发" min-width="130" align="right" />
                <el-table-column label="数量" min-width="100" align="right">
                  <template #default="{ row }">
                    {{ row.quantity === "全部持仓" ? row.quantity : num(row.quantity) }}
                  </template>
                </el-table-column>
                <el-table-column label="来源" width="80">
                  <template #default="{ row }">
                    <span class="text-ap-muted">{{ isOwnOrder(row.client_id) ? "本程序" : "手动" }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="时间" width="130" align="center">
                  <template #default="{ row }">
                    <span class="text-ap-muted">{{ formatTime(new Date(row.time).toISOString()) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>

            <el-card shadow="never" class="sub-card">
              <template #header>
                时间线
              </template>
              <div v-if="events.length" class="ml-1.5 border-l border-l-ap-line border-l-solid">
                <div v-for="e in events" :key="e.id" class="relative pb-3 pl-5 last:pb-1">
                  <i class="absolute left-[-6px] top-1 h-2.5 w-2.5 border-2 border-ap-panel-low border-solid" :class="LEVEL_DOTS[e.level]" />
                  <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span class="text-ap-brand font-bold">{{ formatTime(e.created_at) }}</span>
                    <el-tag size="small" type="info">
                      {{ e.symbol ?? "账户" }}
                    </el-tag>
                    <span :class="e.level === 'error' ? 'text-ap-down' : ''">{{ e.message }}</span>
                    <el-link v-if="e.analysis_id && e.kind !== 'analysis_started'" type="primary" @click="openAnalysis(e.analysis_id)">
                      查看分析
                    </el-link>
                  </div>
                </div>
              </div>
              <el-empty v-else description="还没有事件" :image-size="60" />
              <el-button v-if="events.length && !eventsExhausted" link type="primary" class="mt-1" @click="loadMoreEvents">
                加载更早的事件
              </el-button>
            </el-card>
          </div>
        </el-scrollbar>
      </el-card>
    </div>

    <AddSymbolDialog v-model="addVisible" @added="onAdded" />
    <AnalysisDrawer v-model="drawerVisible" :analysis-id="drawerAnalysisId" />
  </div>
</template>

<style scoped>
.metric {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  font-size: 12px;
}
.metric + .metric::before {
  content: "|";
  margin: 0 10px 0 5px;
  color: var(--ap-line);
}
.metric .key {
  color: var(--ap-muted);
  font-size: 11px;
}
.metric .unit {
  color: var(--ap-muted);
  font-size: 10px;
}
.symbol-item {
  padding: 10px 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--ap-line) 50%, transparent);
  border-left: 2px solid transparent;
  cursor: pointer;
}
.symbol-item:hover {
  background: var(--ap-panel-highest);
}
.symbol-item.active {
  border-left-color: var(--ap-brand);
  background: var(--ap-panel-high);
}
.sub-card {
  --el-card-bg-color: var(--ap-panel-low);
}
.sub-card :deep(.el-table) {
  --el-table-bg-color: var(--ap-panel-low);
  --el-table-tr-bg-color: var(--ap-panel-low);
}
</style>
