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
  if (source.collection_stats?.document_gaps)
    return { type: "warning", text: "存在文档缺口" } as const
  return { type: "info", text: "等待调度" } as const
}
