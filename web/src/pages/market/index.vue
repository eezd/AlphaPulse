<script lang="ts" setup>
import type { CandleType, Chart, DataLoader, KLineData } from "klinecharts"
import type { KlineMessage, TickerMessage } from "./useMarketSocket"
import type { KlineRow, Market, MarketSymbol } from "@/apis/market"
import { dispose, init } from "klinecharts"
import { getTicker, listKlines, listSymbols } from "@/apis/market"
import { usePersisted } from "@/utils/storage"
import { BINANCE_STYLES, DEFAULT_FAVORITE_INTERVALS, DOWN, DRAW_TOOLS, INTERVAL_LABELS, MAIN_INDICATORS, SUB_INDICATORS, TIME_LINE, toPeriod, UP } from "./chart"
import AddSymbolDialog from "./components/AddSymbolDialog.vue"
import SymbolManager from "./components/SymbolManager.vue"
import { useMarketSocket } from "./useMarketSocket"

const PAGE_SIZE = 500
const CANDLE_PANE = "candle_pane"
const SYMBOLS_REFRESH_MS = 5000

const symbols = ref<MarketSymbol[]>([])
const selection = usePersisted<{ market: Market, symbol: string }>("market.selection", { market: "um", symbol: "BTCUSDT" })
const interval = usePersisted("market.interval", "15m")
const favorites = usePersisted("market.favorites", DEFAULT_FAVORITE_INTERVALS)
const mainIndicators = usePersisted("market.mainIndicators", ["MA"])
const subIndicators = usePersisted("market.subIndicators", ["VOL"])
// 指标参数，逗号分隔，如 MA: "7,25,99"
const indicatorParams = usePersisted<Record<string, string>>("market.indicatorParams", { MA: "7,25,99" })
const candleType = usePersisted<CandleType>("market.candleType", "candle_solid")

const addVisible = ref(false)
const managerVisible = ref(false)
const editingFavorites = ref(false)
const chartEl = ref<HTMLElement>()

const current = computed(() => symbols.value.find(s => s.market === selection.value.market && s.symbol === selection.value.symbol))
// “分时”用 1m 数据
const dataInterval = computed(() => interval.value === TIME_LINE ? "1m" : interval.value)
const intervals = computed(() => Object.keys(INTERVAL_LABELS).filter(i => i !== "1s" || selection.value.market === "spot"))
const backfillProgress = computed(() => {
  const progress = current.value?.progress[dataInterval.value]
  return progress && !progress.done && dataInterval.value !== "1s" ? progress.percent : null
})

const ticker = reactive({ last: 0, open: 0, high: 0, low: 0, volume: 0, quoteVolume: 0, changePercent: 0 })
const priceColor = computed(() => ticker.changePercent >= 0 ? UP : DOWN)

let chart: Chart | null = null
let barCallback: ((bar: KLineData) => void) | undefined
// 切换交易对或周期后，丢弃旧请求的返回
let loadVersion = 0
let tickerVersion = 0

function toBar(r: KlineRow): KLineData {
  return { timestamp: r[0], open: r[1], high: r[2], low: r[3], close: r[4], volume: r[5], turnover: r[6] }
}

const dataLoader: DataLoader = {
  async getBars({ type, timestamp, callback }) {
    if (type === "backward") {
      callback([], { backward: false })
      return
    }
    const version = loadVersion
    const { market, symbol } = selection.value
    const rows = await listKlines({
      market,
      symbol,
      interval: dataInterval.value,
      end: type === "forward" ? timestamp ?? undefined : undefined,
      limit: PAGE_SIZE
    })
    if (version === loadVersion)
      callback(rows.map(toBar), { forward: rows.length === PAGE_SIZE, backward: false })
  },
  subscribeBar({ callback }) {
    barCallback = callback
  },
  unsubscribeBar() {
    barCallback = undefined
  }
}

const { connected, subscribe } = useMarketSocket(
  (k: KlineMessage) => barCallback?.({ timestamp: k.t, open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v, turnover: +k.q }),
  (t: TickerMessage) => {
    Object.assign(ticker, { last: +t.c, open: +t.o, high: +t.h, low: +t.l, volume: +t.v, quoteVolume: +t.q })
    ticker.changePercent = ticker.open ? (ticker.last - ticker.open) / ticker.open * 100 : 0
  }
)

async function loadTicker() {
  const version = ++tickerVersion
  const { market, symbol } = selection.value
  const t = await getTicker(market, symbol)
  if (version !== tickerVersion)
    return
  Object.assign(ticker, {
    last: +t.lastPrice,
    high: +t.highPrice,
    low: +t.lowPrice,
    volume: +t.volume,
    quoteVolume: +t.quoteVolume,
    changePercent: +t.priceChangePercent
  })
}

function applyChartState() {
  const s = current.value
  if (!chart || !s)
    return
  loadVersion++
  chart.setStyles({ candle: { type: interval.value === TIME_LINE ? "area" : candleType.value } })
  chart.setSymbol({ ticker: s.symbol, pricePrecision: s.price_precision, volumePrecision: s.volume_precision })
  chart.setPeriod(toPeriod(dataInterval.value))
  subscribe({ market: s.market, symbol: s.symbol, interval: dataInterval.value })
}

function applyIndicators() {
  if (!chart)
    return
  MAIN_INDICATORS.forEach(name => chart!.removeIndicator({ paneId: CANDLE_PANE, name }))
  SUB_INDICATORS.forEach(name => chart!.removeIndicator({ name }))
  const calcParams = (name: string) => {
    const raw = indicatorParams.value[name]?.trim()
    return raw ? { calcParams: raw.split(/[,，\s]+/).map(Number).filter(n => !Number.isNaN(n)) } : {}
  }
  mainIndicators.value.forEach(name => chart!.createIndicator({ name, paneId: CANDLE_PANE, ...calcParams(name) }, true))
  subIndicators.value.forEach(name => chart!.createIndicator({ name, ...calcParams(name) }))
}

function selectSymbol(value: string) {
  const [market, symbol] = value.split(":") as [Market, string]
  selection.value = { market, symbol }
  if (market === "um" && interval.value === "1s")
    interval.value = "1m"
}

function toggleFavorite(i: string) {
  favorites.value = favorites.value.includes(i)
    ? favorites.value.filter(f => f !== i)
    : intervals.value.filter(x => x === i || favorites.value.includes(x))
}

async function refreshSymbols() {
  symbols.value = await listSymbols()
}

function onAdded(market: Market, symbol: string) {
  refreshSymbols().then(() => selectSymbol(`${market}:${symbol}`))
}

function formatNumber(value: number, digits = 2) {
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function formatBig(value: number) {
  if (value >= 1e8)
    return `${(value / 1e8).toFixed(2)}亿`
  if (value >= 1e4)
    return `${(value / 1e4).toFixed(2)}万`
  return formatNumber(value)
}

watch([() => selection.value.market, () => selection.value.symbol], () => {
  applyChartState()
  loadTicker()
})
watch([interval, candleType], applyChartState)
watch([mainIndicators, subIndicators, indicatorParams], applyIndicators, { deep: true })

let resizeObserver: ResizeObserver | undefined
const refreshTimer = setInterval(refreshSymbols, SYMBOLS_REFRESH_MS)

onMounted(async () => {
  await refreshSymbols()
  // 之前选中的交易对已被删除时，优先回到 BTCUSDT
  const fallback = symbols.value.find(s => s.symbol === "BTCUSDT") ?? symbols.value[0]
  if (!current.value && fallback)
    selection.value = { market: fallback.market, symbol: fallback.symbol }
  chart = init(chartEl.value!, { locale: "zh-CN", timezone: "Asia/Shanghai", styles: "dark" })
  chart!.setStyles(BINANCE_STYLES)
  chart!.setDataLoader(dataLoader)
  applyChartState()
  applyIndicators()
  loadTicker()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartEl.value!)
})

onBeforeUnmount(() => {
  clearInterval(refreshTimer)
  resizeObserver?.disconnect()
  if (chartEl.value)
    dispose(chartEl.value)
})
</script>

<template>
  <div class="market h-full flex flex-col overflow-hidden rounded-lg bg-[#161a1e] text-[#eaecef]">
    <!-- 顶栏：交易对与 24h 行情 -->
    <div class="flex flex-wrap items-center gap-6 border-b border-b-solid border-[#2b3139] px-4 py-2">
      <el-select
        :model-value="`${selection.market}:${selection.symbol}`" filterable class="symbol-select w-52"
        @change="selectSymbol"
      >
        <el-option-group v-for="market in (['spot', 'um'] as const)" :key="market" :label="market === 'spot' ? '现货' : 'U 本位永续'">
          <el-option
            v-for="s in symbols.filter(x => x.market === market)" :key="s.id"
            :value="`${s.market}:${s.symbol}`" :label="`${s.base}/${s.quote}${s.market === 'um' ? ' 永续' : ''}`"
          >
            <span>{{ s.base }}/{{ s.quote }}</span>
            <span v-if="s.status !== 'ready'" class="float-right text-xs text-[#f0b90b]">{{ s.status === "error" ? "出错" : "补齐中" }}</span>
          </el-option>
        </el-option-group>
      </el-select>
      <div class="text-xl font-bold" :style="{ color: priceColor }">
        {{ current ? formatNumber(ticker.last, current.price_precision) : "-" }}
      </div>
      <div class="stat">
        <div>24h涨跌</div>
        <div :style="{ color: priceColor }">
          {{ ticker.changePercent >= 0 ? "+" : "" }}{{ ticker.changePercent.toFixed(2) }}%
        </div>
      </div>
      <div class="stat">
        <div>24h最高</div>
        <div>{{ current ? formatNumber(ticker.high, current.price_precision) : "-" }}</div>
      </div>
      <div class="stat">
        <div>24h最低</div>
        <div>{{ current ? formatNumber(ticker.low, current.price_precision) : "-" }}</div>
      </div>
      <div class="stat">
        <div>24h成交量({{ current?.base }})</div>
        <div>{{ formatBig(ticker.volume) }}</div>
      </div>
      <div class="stat">
        <div>24h成交额({{ current?.quote }})</div>
        <div>{{ formatBig(ticker.quoteVolume) }}</div>
      </div>
      <div class="flex-1" />
      <span class="text-xs" :class="connected ? 'text-[#0ecb81]' : 'text-[#f6465d]'">● {{ connected ? "实时" : "断开" }}</span>
      <el-button size="small" type="warning" @click="addVisible = true">
        新增交易对
      </el-button>
      <el-button size="small" @click="managerVisible = true">
        管理
      </el-button>
    </div>

    <!-- 周期与指标工具栏 -->
    <div class="flex items-center gap-1 border-b border-b-solid border-[#2b3139] px-3 py-1 text-sm">
      <span
        v-for="i in favorites.filter(f => intervals.includes(f))" :key="i"
        class="tool" :class="{ active: interval === i }" @click="interval = i"
      >{{ INTERVAL_LABELS[i] }}</span>
      <el-popover placement="bottom-start" :width="300" trigger="click">
        <template #reference>
          <span class="tool" :class="{ active: !favorites.includes(interval) }">
            {{ favorites.includes(interval) ? "更多" : INTERVAL_LABELS[interval] }} ▾
          </span>
        </template>
        <div class="mb-2 flex items-center justify-between text-xs">
          <span>选择周期</span>
          <el-link type="primary" underline="never" @click="editingFavorites = !editingFavorites">
            {{ editingFavorites ? "完成" : "编辑常用" }}
          </el-link>
        </div>
        <div class="grid grid-cols-4 gap-1">
          <el-button
            v-for="i in intervals" :key="i" size="small" class="ml-0!"
            :type="editingFavorites ? (favorites.includes(i) ? 'warning' : 'default') : (interval === i ? 'primary' : 'default')"
            @click="editingFavorites ? toggleFavorite(i) : (interval = i)"
          >
            {{ INTERVAL_LABELS[i] }}
          </el-button>
        </div>
      </el-popover>
      <el-divider direction="vertical" />
      <el-popover placement="bottom-start" :width="360" trigger="click">
        <template #reference>
          <span class="tool">指标 ▾</span>
        </template>
        <div class="mb-1 text-xs text-[var(--el-text-color-secondary)]">
          主图
        </div>
        <el-checkbox-group v-model="mainIndicators" size="small">
          <el-checkbox-button v-for="name in MAIN_INDICATORS" :key="name" :value="name">
            {{ name }}
          </el-checkbox-button>
        </el-checkbox-group>
        <div class="mb-1 mt-3 text-xs text-[var(--el-text-color-secondary)]">
          副图
        </div>
        <el-checkbox-group v-model="subIndicators" size="small">
          <el-checkbox-button v-for="name in SUB_INDICATORS" :key="name" :value="name" class="mb-1">
            {{ name }}
          </el-checkbox-button>
        </el-checkbox-group>
        <div v-if="mainIndicators.length + subIndicators.length" class="mb-1 mt-3 text-xs text-[var(--el-text-color-secondary)]">
          参数（逗号分隔，留空用默认）
        </div>
        <div v-for="name in [...mainIndicators, ...subIndicators]" :key="name" class="mb-1 flex items-center gap-2">
          <span class="w-12 text-xs">{{ name }}</span>
          <el-input :model-value="indicatorParams[name]" size="small" placeholder="默认" @change="(v: string) => indicatorParams[name] = v" />
        </div>
      </el-popover>
      <el-popover placement="bottom-start" :width="200" trigger="click">
        <template #reference>
          <span class="tool">设置 ▾</span>
        </template>
        <el-radio-group v-model="candleType" size="small" class="flex flex-col items-start">
          <el-radio value="candle_solid">
            实心蜡烛
          </el-radio>
          <el-radio value="candle_stroke">
            空心蜡烛
          </el-radio>
          <el-radio value="candle_up_stroke">
            涨空心
          </el-radio>
          <el-radio value="ohlc">
            美国线
          </el-radio>
        </el-radio-group>
      </el-popover>
      <div class="flex-1" />
      <span v-if="backfillProgress !== null" class="text-xs text-[#f0b90b]">
        {{ INTERVAL_LABELS[dataInterval] }} 历史数据补齐中 {{ backfillProgress }}%，最新部分暂未完整
      </span>
    </div>

    <div class="min-h-0 flex flex-1">
      <!-- 画线工具栏 -->
      <div class="flex flex-col gap-1 border-r border-r-solid border-[#2b3139] p-1 text-xs">
        <span v-for="tool in DRAW_TOOLS" :key="tool.name" class="tool text-center" :title="tool.label" @click="chart?.createOverlay(tool.name)">
          {{ tool.label }}
        </span>
        <span class="tool text-center text-[#f6465d]" title="清除全部画线" @click="chart?.removeOverlay()">清除</span>
      </div>
      <div ref="chartEl" class="min-w-0 flex-1" />
    </div>

    <AddSymbolDialog v-model="addVisible" @added="onAdded" />
    <SymbolManager v-model="managerVisible" :symbols="symbols" @changed="refreshSymbols" />
  </div>
</template>

<style scoped>
.stat {
  font-size: 12px;
}
.stat > div:first-child {
  color: #848e9c;
}
.tool {
  padding: 2px 8px;
  border-radius: 4px;
  color: #848e9c;
  cursor: pointer;
  white-space: nowrap;
}
.tool:hover {
  color: #eaecef;
  background: #2b3139;
}
.tool.active {
  color: #f0b90b;
}
.symbol-select :deep(.el-select__wrapper) {
  background: #2b3139;
  box-shadow: none;
}
.symbol-select :deep(.el-select__selected-item) {
  color: #eaecef;
  font-weight: 600;
}
</style>
