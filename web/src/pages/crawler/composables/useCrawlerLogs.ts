import type { CrawlLog } from "@@/apis/crawler/type"
import { listLogsApi, subscribeCrawlerApi } from "@@/apis/crawler"
import { useEventSource } from "@/http/useEventSource"

export function useCrawlerLogs(onStarted: (code: string) => void) {
  const MAX_LOGS = 1000
  const LEVELS = ["INFO", "WARNING", "ERROR"]
  const logs = reactive<Record<string, CrawlLog[]>>({})
  const logSource = ref("")
  const levels = ref([...LEVELS])
  const autoScroll = ref(true)
  const logBox = ref<HTMLElement>()
  // 各来源日志按全局自增 id 合并
  const visibleLogs = computed(() => Object.values(logs).flat().filter(log => (!logSource.value || log.source_code === logSource.value)
    && (levels.value.includes(log.level) || (log.level === "CRITICAL" && levels.value.includes("ERROR")))).sort((a, b) => a.id - b.id).slice(-MAX_LOGS))

  watch(() => visibleLogs.value.at(-1)?.id, async () => {
    if (!autoScroll.value)
      return
    await nextTick()
    logBox.value?.scrollTo({ top: logBox.value.scrollHeight })
  })

  function levelClass(level: string) {
    if (level === "WARNING")
      return "bg-amber-500/20 text-amber-300"
    if (level === "ERROR" || level === "CRITICAL")
      return "bg-rose-500/20 text-rose-300"
    return "bg-sky-500/20 text-sky-300"
  }

  function appendLog(log: CrawlLog) {
    const list = (logs[log.source_code] ??= [])
    // SSE 可能早于历史日志到达，按 id 去重
    if (list.length && list.at(-1)!.id >= log.id)
      return
    list.push(log)
    if (list.length > MAX_LOGS)
      list.splice(0, list.length - MAX_LOGS)
    if (log.message.startsWith("子进程已启动"))
      onStarted(log.source_code)
  }

  const { connected } = useEventSource<CrawlLog>(subscribeCrawlerApi, appendLog)

  async function loadHistory(code: string) {
    const history = await listLogsApi(code, 200)
    const live = logs[code] ?? []
    const firstLiveId = live[0]?.id ?? Infinity
    logs[code] = [...history.filter(log => log.id < firstLiveId), ...live]
  }

  return { logs, logSource, levels, autoScroll, logBox, visibleLogs, levelClass, connected, loadHistory, LEVELS }
}
