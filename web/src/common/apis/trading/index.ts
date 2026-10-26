import type * as Trading from "./type"
import { request } from "@/http/axios"

/** 读取模拟盘账户与全局分析状态 */
export function getStatusApi() {
  return request<Trading.GetStatusResponseData>({
    url: "/trading/status",
    method: "get"
  })
}

/** 开始指定币种的自动交易 */
export function startTradingApi(symbol: string) {
  return request<Trading.StartTradingResponseData>({
    url: `/trading/symbols/${symbol}/start`,
    method: "post"
  })
}

/** 停止指定币种的新分析并撤销入场单 */
export function stopTradingApi(symbol: string) {
  return request<Trading.StopTradingResponseData>({
    url: `/trading/symbols/${symbol}/stop`,
    method: "post"
  })
}

/** 设置该币的交易资金：各币合计不超过账户权益 */
export function patchSymbolApi(symbol: string, body: Trading.PatchSymbolBodyRequestData) {
  return request<Trading.PatchSymbolResponseData>({
    url: `/trading/symbols/${symbol}`,
    method: "patch",
    data: body
  })
}

/** 更新指定的全局交易配置字段 */
export function patchConfigApi(body: Trading.PatchConfigBodyRequestData) {
  return request<Trading.PatchConfigResponseData>({
    url: "/trading/config",
    method: "patch",
    data: body
  })
}

/** 查询模拟盘可交易币种 */
export function listSymbolOptionsApi() {
  return request<Trading.ListSymbolOptionsResponseData>({
    url: "/trading/symbol-options",
    method: "get"
  })
}

/** 读取交易币种列表 */
export function listSymbolsApi() {
  return request<Trading.ListSymbolsResponseData>({
    url: "/trading/symbols",
    method: "get"
  })
}

/** 新增交易币种 */
export function addSymbolApi(symbol: string) {
  return request<Trading.AddSymbolResponseData>({
    url: "/trading/symbols",
    method: "post",
    data: { symbol }
  })
}

/** 移出交易币种，保留历史 */
export function deleteSymbolApi(symbol: string) {
  return request<Trading.DeleteSymbolResponseData>({
    url: `/trading/symbols/${symbol}`,
    method: "delete"
  })
}

/** 分页读取交易时间线 */
export function listEventsApi(params: Trading.ListEventsParamsRequestData) {
  return request<Trading.ListEventsResponseData>({
    url: "/trading/events",
    method: "get",
    params
  })
}

/** 筛选已平仓历史及汇总 */
export function listPositionHistoryApi(params: Trading.PositionHistoryQuery) {
  return request<Trading.ListPositionHistoryResponseData>({
    url: "/trading/positions/history",
    method: "get",
    params
  })
}

/** 读取历史仓位币种 */
export function listPositionHistorySymbolsApi() {
  return request<Trading.ListPositionHistorySymbolsResponseData>({
    url: "/trading/positions/history/symbols",
    method: "get"
  })
}

/** 读取新闻信号前向统计 */
export function getNewsSignalsApi() {
  return request<Trading.GetNewsSignalsResponseData>({
    url: "/trading/news-signals",
    method: "get"
  })
}

/** 读取当前执行计划与订单流水 */
export function getSymbolPlanApi(symbol: string) {
  return request<Trading.GetSymbolPlanResponseData>({
    url: `/trading/symbols/${symbol}/plan`,
    method: "get"
  })
}

/** 排队手动分析；202 仅表示已受理 */
export function requestAnalysisApi(symbol: string) {
  return request<Trading.RequestAnalysisResponseData>({
    url: `/trading/symbols/${symbol}/analyze`,
    method: "post"
  })
}

/** 一键平仓（同时停止该币的行情交易）、清除挂单：由执行器在几秒内执行，结果写入时间线 */
export function manualActionApi(symbol: string, action: "close" | "cancel") {
  return request<Trading.ManualActionResponseData>({
    url: `/trading/symbols/${symbol}/manual/${action}`,
    method: "post"
  })
}

/** 读取分析记录列表 */
export function listAnalysesApi(params: Trading.ListAnalysesParamsRequestData) {
  return request<Trading.ListAnalysesResponseData>({
    url: "/trading/analyses",
    method: "get",
    params
  })
}

/** 读取分析详情与工具调用过程 */
export function getAnalysisApi(id: number) {
  return request<Trading.GetAnalysisResponseData>({
    url: `/trading/analyses/${id}`,
    method: "get"
  })
}
