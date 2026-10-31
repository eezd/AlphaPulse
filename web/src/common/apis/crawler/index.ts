import type * as Crawler from "./type"
import { request } from "@/http/axios"
import { consumeStream } from "@/http/stream"

/** 读取新闻与日历来源状态 */
export function listSourcesApi() {
  return request<Crawler.ListSourcesResponseData>({
    url: "/crawler/sources",
    method: "get"
  })
}

/** 修改新闻来源设置 */
export function patchSourceApi(code: string, body: Crawler.SourcePatch) {
  return request<Crawler.PatchSourceResponseData>({
    url: `/crawler/sources/${code}`,
    method: "patch",
    data: body
  })
}

/** 读取来源历史日志 */
export function listLogsApi(source: string, limit = 200) {
  return request<Crawler.ListLogsResponseData>({
    url: "/crawler/logs",
    method: "get",
    params: { source, limit }
  })
}

/** 请求重启新闻采集 */
export function restartCrawlerApi(source: string) {
  return request<Crawler.RestartCrawlerResponseData>({
    url: "/crawler/restart",
    method: "post",
    data: { source }
  })
}

/** 只读订阅，重连由订阅生命周期负责。 */
export function subscribeCrawlerApi(signal: AbortSignal, version: number, onOpen: () => void, onMessage: (data: Crawler.CrawlLog) => void) {
  return consumeStream<Crawler.CrawlLog>("/api/crawler/logs/stream", { signal }, version, onOpen, (name, data) => {
    if (name === "log") onMessage(data)
  })
}

export const createRssApi = (data: Crawler.RssInput) => request<{ code: string }>({ url: "/crawler/sources/rss", method: "post", data })
