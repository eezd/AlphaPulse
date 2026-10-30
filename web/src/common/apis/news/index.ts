import type * as News from "./type"
import { request } from "@/http/axios"
import { consumeStream } from "@/http/stream"

/** 分页查询新闻 */
export function listNewsApi(params: News.NewsQuery, notify = true) {
  return request<News.ListNewsResponseData>({
    url: "/news",
    method: "get",
    params,
    notify,
    paramsSerializer: { indexes: null }
  })
}

/** 读取新闻详情 */
export function getNewsApi(id: number) {
  return request<News.GetNewsResponseData>({
    url: `/news/${id}`,
    method: "get"
  })
}

/** 读取新闻修订记录 */
export function listRevisionsApi(id: number) {
  return request<News.ListRevisionsResponseData>({
    url: `/news/${id}/revisions`,
    method: "get"
  })
}

/** 只读订阅，重连由订阅生命周期负责。 */
export function subscribeNewsApi(signal: AbortSignal, version: number, onOpen: () => void, onMessage: (data: News.NewsEvent) => void) {
  return consumeStream<News.NewsEvent>("/api/news/stream", { signal }, version, onOpen, (name, data) => {
    if (name === "news") onMessage(data)
  })
}
