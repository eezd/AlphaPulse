import type { Analysis, OrderRow, TradingEvent, TradingStatus, TradingSymbol } from "@@/apis/trading/type"
import { getStatusApi, getSymbolPlanApi, listAnalysesApi, listEventsApi, listSymbolsApi } from "@@/apis/trading"
import { usePersisted } from "@@/utils/storage"
import { logRequestError } from "@/http/axios"

export function useTradingData() {
  const EVENTS_PAGE = 50
  const status = ref<TradingStatus>()
  const symbols = ref<TradingSymbol[]>([])
  const selected = usePersisted("trading.symbol", "")
  const events = ref<TradingEvent[]>([])
  const eventsExhausted = ref(false)
  const latestAnalysis = ref<Analysis | null>(null)
  const livePlan = ref<Analysis | null>(null)
  const planOrders = ref<OrderRow[]>([])
  const current = computed(() => symbols.value.find(s => s.symbol === selected.value))
  const loading = ref(false)
  const failed = ref(false)
  let refreshVersion = 0
  let analysisVersion = 0
  async function refresh() {
    const previous = `${status.value?.exchange}:${selected.value}`
    const version = ++refreshVersion
    loading.value = status.value === undefined
    try {
      const [nextStatus, nextSymbols] = await Promise.all([getStatusApi(), listSymbolsApi()])
      if (version !== refreshVersion) return
      status.value = nextStatus
      symbols.value = nextSymbols
      failed.value = false
    } catch (error) {
      if (version === refreshVersion) failed.value = true
      throw error
    } finally {
      if (version === refreshVersion) loading.value = false
    }
    if (!current.value && symbols.value.length)
      selected.value = symbols.value[0].symbol
    if (previous === `${status.value?.exchange}:${selected.value}`) await refreshAnalysis()
  }

  async function refreshAnalysis() {
    const version = ++analysisVersion
    const exchange = status.value?.exchange
    const symbol = selected.value
    if (!symbol)
      return
    const [latest, { plan, orders }] = await Promise.all([listAnalysesApi({ symbol, limit: 1 }), getSymbolPlanApi(symbol)])
    if (version !== analysisVersion || symbol !== selected.value || exchange !== status.value?.exchange)
      return
    latestAnalysis.value = latest[0] ?? null
    livePlan.value = plan
    planOrders.value = orders
  }

  async function refreshEvents() {
    const exchange = status.value?.exchange
    const symbol = selected.value
    const latest = await listEventsApi({ symbol: symbol || undefined, limit: EVENTS_PAGE })
    // 请求期间切换了币种：丢弃旧币种的结果
    if (symbol !== selected.value || exchange !== status.value?.exchange)
      return
    const newestId = events.value[0]?.id ?? 0
    const fresh = latest.filter(e => e.id > newestId)
    if (!events.value.length)
      eventsExhausted.value = latest.length < EVENTS_PAGE
    events.value = [...fresh, ...events.value]
  }

  async function loadMoreEvents() {
    const exchange = status.value?.exchange
    const symbol = selected.value
    const older = await listEventsApi({ symbol: symbol || undefined, before: events.value.at(-1)?.id, limit: EVENTS_PAGE })
    if (symbol !== selected.value || exchange !== status.value?.exchange)
      return
    eventsExhausted.value = older.length < EVENTS_PAGE
    events.value = [...events.value, ...older]
  }

  watch([selected, () => status.value?.exchange], () => {
    events.value = []
    latestAnalysis.value = null
    livePlan.value = null
    planOrders.value = []
    void Promise.all([refreshEvents(), refreshAnalysis()]).catch(logRequestError)
  })
  onMounted(refresh)
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  async function poll() {
    try {
      await Promise.all([refresh(), refreshEvents()])
    } catch (error) {
      logRequestError(error)
    } finally {
      if (!stopped) timer = setTimeout(poll, 3000)
    }
  }
  timer = setTimeout(poll, 3000)
  onBeforeUnmount(() => {
    stopped = true
    ++refreshVersion
    ++analysisVersion
    clearTimeout(timer)
  })
  return { status, symbols, selected, events, eventsExhausted, latestAnalysis, livePlan, planOrders, current, loading, failed, refresh, refreshAnalysis, refreshEvents, loadMoreEvents }
}
