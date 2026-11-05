import type { SourceInfo } from "@@/apis/crawler/type"

export function sourceStatus(source: SourceInfo, restarting = false, now = Date.now()) {
  if (source.configuration_error)
    return { type: "warning", text: source.configuration_error } as const
  if (!source.enabled)
    return { type: "info", text: "已停用" } as const
  if (restarting || source.process?.state === "restarting")
    return { type: "warning", text: "重启中" } as const
  if (!source.process || source.process.state === "stopped")
    return { type: "danger", text: "采集进程未运行" } as const
  if (source.paused_until && new Date(source.paused_until).getTime() > now)
    return { type: "warning", text: "失败退避中" } as const
  if (source.collection_stats?.round_running)
    return { type: "primary", text: "采集中" } as const
  if (source.consecutive_failures)
    return { type: "danger", text: "失败待重试" } as const
  if (source.collection_stats?.document_gaps || source.collection_stats?.attachment_gaps || source.collection_stats?.unresolved_bills)
    return { type: "warning", text: "存在采集缺口" } as const
  return { type: "info", text: "等待调度" } as const
}

function originalUrl(url: string) {
  const bill = /^https:\/\/api\.congress\.gov\/v3\/bill\/(\d+)\/([a-z]+)\/(\d+)\/?(?:\?[^#]*)?(?:#.*)?$/i.exec(url)
  const billTypes: Record<string, string> = {
    hr: "house-bill",
    s: "senate-bill",
    hjres: "house-joint-resolution",
    sjres: "senate-joint-resolution",
    hconres: "house-concurrent-resolution",
    sconres: "senate-concurrent-resolution",
    hres: "house-resolution",
    sres: "senate-resolution"
  }
  if (bill && billTypes[bill[2].toLowerCase()])
    return `https://www.congress.gov/bill/${bill[1]}th-congress/${billTypes[bill[2].toLowerCase()]}/${bill[3]}`
  return url
}

export function collectionGaps(source: Pick<SourceInfo, "history_cursor">) {
  const groups = [
    ["document_retries", "正文"],
    ["attachment_retries", "附件"],
    ["bill_retries", "法案"]
  ] as const
  return groups.flatMap(([key, label]) => Object.entries(source.history_cursor?.[key] ?? {}).map(([url, retry]) => ({
    key: `${key}:${url}`,
    label,
    url: originalUrl(retry.document.canonical ?? retry.document.url),
    error: retry.error || "历史记录未保存具体原因，请核对原文",
    attempts: retry.attempts,
    exhausted: retry.attempts >= 3,
    nextAt: new Date(retry.next_at * 1000).toISOString()
  })))
}
