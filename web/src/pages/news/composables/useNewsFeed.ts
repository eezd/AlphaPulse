import type { NewsBrief, NewsEvent } from "@@/apis/news/type"
import { listNewsApi, subscribeNewsApi } from "@@/apis/news"
import { useSessionScope } from "@@/composables/useSessionScope"
import { useSourceNames } from "@@/composables/useSourceNames"
import { usePersisted } from "@@/utils/storage"
import { useEventSource } from "@/http/useEventSource"

export function useNewsFeed() {
  const scope = useSessionScope()
  const sourceNames = useSourceNames()
  const filters = reactive({
    source: [] as string[],
    q: "",
    range: null as [Date, Date] | null
  })
  const items = ref<NewsBrief[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = usePersisted("news-page-size", 30)
  const loading = ref(false)
  const failed = ref(false)
  const riskFailed = ref(false)
  let riskLoading = false
  let timer: ReturnType<typeof setInterval> | undefined
  const pendingCount = ref(0)
  const openedId = ref<number | null>(null)
  const listBox = ref<HTMLElement>()

  // 多次快速翻页时只采用最后一次请求的结果
  let requestSeq = 0

  async function load() {
    const seq = ++requestSeq
    loading.value = true
    try {
      const result = await listNewsApi({
        source: filters.source.length ? filters.source : undefined,
        q: filters.q || undefined,
        start: filters.range?.[0].toISOString(),
        end: filters.range?.[1].toISOString(),
        page: page.value,
        limit: pageSize.value
      })
      if (seq !== requestSeq)
        return
      failed.value = false
      riskFailed.value = false
      items.value = result.items
      total.value = result.total
      if (page.value === 1)
        pendingCount.value = 0
      listBox.value?.scrollTo({ top: 0 })
    } catch (error) {
      if (seq === requestSeq) failed.value = true
      throw error
    } finally {
      if (seq === requestSeq)
        loading.value = false
    }
  }

  function search() {
    if (page.value !== 1) page.value = 1
    else return load()
  }

  async function refreshRisk() {
    if (!scope.isCurrent() || loading.value || riskLoading || !items.value.length) return
    const seq = requestSeq
    riskLoading = true
    try {
      const result = await listNewsApi({ ids: items.value.map(item => item.id), limit: 100 }, false)
      if (seq !== requestSeq || !scope.isCurrent()) return
      const latest = new Map(result.items.map(item => [item.id, item]))
      for (const item of items.value) {
        const current = latest.get(item.id)
        if (current?.revision_no === item.revision_no) item.risk = current.risk
      }
      riskFailed.value = false
    } catch {
      if (seq === requestSeq && scope.isCurrent()) riskFailed.value = true
    } finally {
      riskLoading = false
    }
  }

  watch([page, pageSize], ([, size], [, oldSize]) => {
    if (size !== oldSize && page.value !== 1) {
      page.value = 1
      return
    }
    return load()
  })

  // 只提示符合当前来源筛选的新条目；关键字和时间范围由刷新后的查询决定
  const { connected } = useEventSource<NewsEvent>(subscribeNewsApi, (event) => {
    if (event.kind === "new" && (filters.source.length === 0 || filters.source.includes(event.source_code)))
      pendingCount.value++
  })

  onMounted(load)
  onMounted(() => {
    timer = setInterval(() => {
      if (!document.hidden && items.value.some(item => item.risk?.state !== "done")) void refreshRisk()
    }, 15000)
  })
  onBeforeUnmount(() => {
    requestSeq++
    clearInterval(timer)
  })

  return { sourceNames, filters, items, total, page, pageSize, loading, failed, riskFailed, pendingCount, openedId, listBox, connected, load, search, refreshRisk }
}
