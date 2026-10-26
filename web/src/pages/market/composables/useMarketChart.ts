import type { KlineMessage, KlineRow, Market, MarketSymbol, TickerMessage } from "@@/apis/market/type"
import type { CandleType, Chart, DataLoader, KLineData } from "klinecharts"
import { getTickerApi, listKlinesApi, listSymbolsApi } from "@@/apis/market"
import { usePersisted } from "@@/utils/storage"
import { dispose, init } from "klinecharts"
import { logRequestError } from "@/http/axios"
import { BINANCE_STYLES, DEFAULT_FAVORITE_INTERVALS, DOWN, INTERVAL_LABELS, MAIN_INDICATORS, SUB_INDICATORS, TIME_LINE, toPeriod, UP } from "../chart"
import { useMarketSocket } from "./useMarketSocket"

export function useMarketChart() {
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

  const chart = shallowRef<Chart | null>(null)
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
      const rows = await listKlinesApi({
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
    const t = await getTickerApi(market, symbol)
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
    if (!chart.value || !s)
      return
    loadVersion++
    chart.value.setStyles({ candle: { type: interval.value === TIME_LINE ? "area" : candleType.value } })
    chart.value.setSymbol({ ticker: s.symbol, pricePrecision: s.price_precision, volumePrecision: s.volume_precision })
    chart.value.setPeriod(toPeriod(dataInterval.value))
    subscribe({ market: s.market, symbol: s.symbol, interval: dataInterval.value })
  }

  function applyIndicators() {
    if (!chart.value)
      return
    MAIN_INDICATORS.forEach(name => chart.value!.removeIndicator({ paneId: CANDLE_PANE, name }))
    SUB_INDICATORS.forEach(name => chart.value!.removeIndicator({ name }))
    const calcParams = (name: string) => {
      const raw = indicatorParams.value[name]?.trim()
      return raw ? { calcParams: raw.split(/[,，\s]+/).map(Number).filter(n => !Number.isNaN(n)) } : {}
    }
    mainIndicators.value.forEach(name => chart.value!.createIndicator({ name, paneId: CANDLE_PANE, ...calcParams(name) }, true))
    subIndicators.value.forEach(name => chart.value!.createIndicator({ name, ...calcParams(name) }))
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
    symbols.value = await listSymbolsApi()
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

  let disposed = false
  let resizeObserver: ResizeObserver | undefined
  const refreshTimer = setInterval(() => refreshSymbols().catch(logRequestError), SYMBOLS_REFRESH_MS)

  onMounted(async () => {
    await refreshSymbols()
    if (disposed) return
    // 之前选中的交易对已被删除时，优先回到 BTCUSDT
    const fallback = symbols.value.find(s => s.symbol === "BTCUSDT") ?? symbols.value[0]
    if (!current.value && fallback)
      selection.value = { market: fallback.market, symbol: fallback.symbol }
    chart.value = init(chartEl.value!, { locale: "zh-CN", timezone: "Asia/Shanghai", styles: "dark" })
    chart.value!.setStyles(BINANCE_STYLES)
    chart.value!.setDataLoader(dataLoader)
    applyChartState()
    applyIndicators()
    await loadTicker()
    if (disposed) return
    resizeObserver = new ResizeObserver(() => chart.value?.resize())
    resizeObserver.observe(chartEl.value!)
  })

  onBeforeUnmount(() => {
    disposed = true
    ++loadVersion
    ++tickerVersion
    clearInterval(refreshTimer)
    resizeObserver?.disconnect()
    if (chartEl.value)
      dispose(chartEl.value)
  })

  return { symbols, selection, interval, favorites, mainIndicators, subIndicators, indicatorParams, candleType, chartEl, chart, current, dataInterval, intervals, backfillProgress, ticker, priceColor, connected, loadTicker, selectSymbol, toggleFavorite, refreshSymbols, onAdded, formatNumber, formatBig }
}
