<script lang="ts" setup>
import type { Effort, Provider } from "@/apis/settings"
import type { Analysis, Angle, SnapshotPosition, TradingEvent, TradingStatus, TradingSymbol } from "@/apis/trading"
import { EFFORTS, listProviders } from "@/apis/settings"
import { ANGLE_LABELS, deleteSymbol, getStatus, isOwnOrder, listAnalyses, listEvents, listSymbols, patchConfig, patchSymbol, requestAnalysis, startTrading, stopTrading } from "@/apis/trading"
import { usePersisted } from "@/utils/storage"
import { formatTime } from "@/utils/text"
import AddSymbolDialog from "./components/AddSymbolDialog.vue"
import AnalysisDrawer from "./components/AnalysisDrawer.vue"
import PlanCard from "./components/PlanCard.vue"

const STATUS_REFRESH_MS = 3000
const EVENTS_PAGE = 50
const SHORT_ANGLE_LABELS: Record<Angle, string> = { short: "短线", long: "长线" }
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

function num(value: string) {
  return String(Number(value))
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

function symbolState(s: TradingSymbol) {
  if (s.analyzing)
    return { label: "分析中", type: "warning" } as const
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
  const latest = symbol ? (await listAnalyses({ symbol, limit: 1 }))[0] ?? null : null
  if (symbol === selected.value)
    latestAnalysis.value = latest
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
  refreshEvents()
  refreshAnalysis()
})

async function toggleRunning() {
  if (status.value!.running) {
    await ElMessageBox.confirm(
      "停止后不再做新的分析，并撤销未成交的入场单。已有持仓保留，止损、止盈、到期平仓、短线失效平仓等程序规则继续执行。",
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
    `仓位按交易资金计算：单笔风险短线 1%、长线 2%，所有仓位名义价值合计不超过 3 倍（逐仓 3 倍时保证金合计不超过交易资金）。不能超过账户权益 ${money(account.value?.margin_balance)} USDT；账户权益低于它时按权益计算。`,
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

async function switchAngle(s: TradingSymbol) {
  const angle: Angle = s.angle === "short" ? "long" : "short"
  await ElMessageBox.confirm(`把 ${s.symbol} 从${SHORT_ANGLE_LABELS[s.angle]}改为${SHORT_ANGLE_LABELS[angle]}？`, "切换角度")
  await patchSymbol(s.symbol, angle)
  await Promise.all([refresh(), refreshEvents()])
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
  <div v-if="status" class="h-full flex flex-col gap-3">
    <!-- 顶部：引擎状态、启停、账户 -->
    <div class="flex flex-wrap items-center gap-x-6 gap-y-2 border border-[var(--el-border-color)] rounded border-solid px-4 py-3">
      <div class="flex items-center gap-2">
        <el-tag v-if="snapshot" type="success" effect="dark">
          引擎在线
        </el-tag>
        <el-tag v-else type="info" effect="dark">
          引擎离线
        </el-tag>
        <el-tag :type="status.running ? 'success' : 'info'">
          {{ status.running ? "交易中" : "已停止" }}
        </el-tag>
        <el-button
          :type="status.running ? 'danger' : 'primary'" :loading="toggling"
          :disabled="!status.running && (!status.configured || !status.capital_usdt)"
          @click="toggleRunning"
        >
          {{ status.running ? "停止" : "开始行情交易" }}
        </el-button>
      </div>
      <div class="stat">
        <span>交易资金</span>
        <template v-if="status.capital_usdt">
          {{ money(status.capital_usdt) }} USDT
          <span v-if="status.effective_capital && Number(status.effective_capital) < Number(status.capital_usdt)" class="text-[var(--el-color-warning)]!">
            （权益不足，按 {{ money(status.effective_capital) }}）
          </span>
        </template>
        <b v-else class="text-[var(--el-color-danger)]">未设置</b>
        <el-link type="primary" underline="never" class="ml-1" @click="editCapital">
          设置
        </el-link>
      </div>
      <div class="stat">
        <span>账户权益</span>{{ money(account?.margin_balance) }} USDT
      </div>
      <div class="stat">
        <span>可用</span>{{ money(account?.available_balance) }} USDT
      </div>
      <div class="stat">
        <span>未实现盈亏</span>
        <b :class="pnlClass(account?.unrealized_pnl ?? '0')">{{ money(account?.unrealized_pnl) }}</b>
      </div>
      <div class="stat">
        <span>今日分析</span>{{ status.today.analyses }} / {{ status.today.limit }}
        <span class="ml-1">token</span>{{ status.today.input_tokens.toLocaleString() }} / {{ status.today.output_tokens.toLocaleString() }}
      </div>
      <div class="flex-1" />
      <el-select
        :model-value="status.provider_id" class="w-56" clearable :value-on-clear="null" placeholder="模型：跟随默认配置"
        @update:model-value="changeProvider"
      >
        <el-option v-for="p in providers" :key="p.id" :value="p.id" :label="`${p.name}（${p.model}）`" />
      </el-select>
      <el-select
        :model-value="status.reasoning_effort" class="w-40" clearable :value-on-clear="null" placeholder="思考强度：跟随配置"
        @update:model-value="changeEffort"
      >
        <el-option v-for="e in EFFORTS" :key="e" :value="e" :label="e" />
      </el-select>
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
    <el-table :data="positions" border size="small" empty-text="没有持仓">
      <el-table-column prop="symbol" label="币种" min-width="110" />
      <el-table-column label="方向" width="70">
        <template #default="{ row }">
          <el-tag :type="row.side === 'LONG' ? 'success' : 'danger'" size="small">
            {{ row.side === "LONG" ? "多" : "空" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="数量" min-width="90">
        <template #default="{ row }">
          {{ num(row.amount) }}
        </template>
      </el-table-column>
      <el-table-column label="开仓价" min-width="100">
        <template #default="{ row }">
          {{ num(row.entry_price) }}
        </template>
      </el-table-column>
      <el-table-column label="保本价" min-width="100">
        <template #default="{ row }">
          {{ Number(row.break_even_price).toFixed(2) }}
        </template>
      </el-table-column>
      <el-table-column label="标记价" min-width="100">
        <template #default="{ row }">
          {{ Number(row.mark_price).toFixed(2) }}
        </template>
      </el-table-column>
      <el-table-column label="未实现盈亏" min-width="100">
        <template #default="{ row }">
          <span :class="pnlClass(row.unrealized_pnl)">{{ money(row.unrealized_pnl) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="强平价" min-width="100">
        <template #default="{ row }">
          {{ Number(row.liquidation_price) ? Number(row.liquidation_price).toFixed(2) : "-" }}
        </template>
      </el-table-column>
      <el-table-column label="杠杆" width="110">
        <template #default="{ row }">
          {{ row.leverage }}x {{ row.margin_type === "ISOLATED" ? "逐仓" : "全仓" }}
        </template>
      </el-table-column>
      <el-table-column label="止损 / 止盈" min-width="140">
        <template #default="{ row }">
          {{ exitPrices(row as SnapshotPosition).stop }} / {{ exitPrices(row as SnapshotPosition).target }}
        </template>
      </el-table-column>
    </el-table>

    <div class="min-h-0 flex flex-1 gap-3">
      <!-- 侧边栏：交易币种 -->
      <div class="w-60 flex flex-col border border-[var(--el-border-color)] rounded border-solid">
        <div class="flex items-center justify-between border-b border-b-[var(--el-border-color)] border-b-solid px-3 py-2">
          <span class="font-bold">交易币种</span>
          <el-button size="small" type="primary" @click="addVisible = true">
            新增
          </el-button>
        </div>
        <el-scrollbar class="flex-1">
          <div
            v-for="s in symbols" :key="s.symbol"
            class="cursor-pointer border-b border-b-[var(--el-border-color-lighter)] border-b-solid px-3 py-2"
            :class="s.symbol === selected ? 'bg-[var(--el-color-primary-light-9)]' : ''"
            @click="selected = s.symbol"
          >
            <div class="flex items-center justify-between">
              <span class="font-bold">{{ s.symbol }}</span>
              <el-tag :type="symbolState(s).type" size="small">
                {{ symbolState(s).label }}
              </el-tag>
            </div>
            <div class="mt-1 flex items-center justify-between text-xs text-[var(--el-text-color-secondary)]">
              <span>{{ ANGLE_LABELS[s.angle] }}</span>
              <span>
                <el-link type="primary" underline="never" class="text-xs!" @click.stop="switchAngle(s)">切换角度</el-link>
                <el-link type="danger" underline="never" class="ml-2 text-xs!" @click.stop="remove(s)">删除</el-link>
              </span>
            </div>
          </div>
          <el-empty v-if="!symbols.length" description="还没有交易币种" :image-size="60" />
        </el-scrollbar>
      </div>

      <!-- 主区域：当前计划、挂单、时间线 -->
      <el-scrollbar class="flex-1">
        <div class="flex flex-col gap-3 pr-2">
          <PlanCard
            v-if="current" :symbol="current" :analysis="latestAnalysis" :requesting="requesting"
            @analyze="analyze" @detail="openAnalysis"
          />

          <div v-if="current">
            <div class="mb-2 font-bold">
              当前挂单
            </div>
            <el-table :data="symbolOrders" border size="small" empty-text="没有挂单">
              <el-table-column label="类型" min-width="150">
                <template #default="{ row }">
                  {{ row.type }}
                  <el-tag v-if="row.conditional" size="small" class="ml-1">
                    条件单
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="方向" width="110">
                <template #default="{ row }">
                  {{ row.side === "BUY" ? "买" : "卖" }}{{ row.reduce_only ? "（只减仓）" : "" }}
                </template>
              </el-table-column>
              <el-table-column prop="priceText" label="价格" min-width="130" />
              <el-table-column label="数量" min-width="100">
                <template #default="{ row }">
                  {{ row.quantity === "全部持仓" ? row.quantity : num(row.quantity) }}
                </template>
              </el-table-column>
              <el-table-column label="来源" width="80">
                <template #default="{ row }">
                  {{ isOwnOrder(row.client_id) ? "本程序" : "手动" }}
                </template>
              </el-table-column>
              <el-table-column label="时间" width="130">
                <template #default="{ row }">
                  {{ formatTime(new Date(row.time).toISOString(), "MM-DD HH:mm:ss") }}
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div>
            <div class="mb-2 font-bold">
              时间线
            </div>
            <el-timeline v-if="events.length" class="pl-1">
              <el-timeline-item
                v-for="e in events" :key="e.id" :timestamp="formatTime(e.created_at)" :type="LEVEL_TYPES[e.level]"
                placement="top"
              >
                <el-tag size="small" type="info" class="mr-2">
                  {{ e.symbol ?? "账户" }}
                </el-tag>
                <span :class="e.level === 'error' ? 'text-[var(--el-color-danger)]' : ''">{{ e.message }}</span>
                <el-link v-if="e.analysis_id && e.kind !== 'analysis_started'" type="primary" underline="never" class="ml-2" @click="openAnalysis(e.analysis_id)">
                  查看分析
                </el-link>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-else description="还没有事件" :image-size="60" />
            <el-button v-if="events.length && !eventsExhausted" link type="primary" @click="loadMoreEvents">
              加载更早的事件
            </el-button>
          </div>
        </div>
      </el-scrollbar>
    </div>

    <AddSymbolDialog v-model="addVisible" @added="onAdded" />
    <AnalysisDrawer v-model="drawerVisible" :analysis-id="drawerAnalysisId" />
  </div>
</template>

<style scoped>
.stat {
  font-size: 14px;
}
.stat span {
  margin-right: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
