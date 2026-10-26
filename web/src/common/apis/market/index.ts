import type * as Market from "./type"
import { request } from "@/http/axios"

/** 读取研究行情交易对 */
export function listSymbolsApi() {
  return request<Market.ListSymbolsResponseData>({
    url: "/market/symbols",
    method: "get"
  })
}

/** 新增研究行情交易对 */
export function addSymbolApi(market: Market.Market, symbol: string) {
  return request<Market.AddSymbolResponseData>({
    url: "/market/symbols",
    method: "post",
    data: { market, symbol }
  })
}

/** 移出研究行情交易对 */
export function deleteSymbolApi(id: number) {
  return request<Market.DeleteSymbolResponseData>({
    url: `/market/symbols/${id}`,
    method: "delete"
  })
}

/** 查询交易所可用交易对 */
export function listTradableApi(market: Market.Market) {
  return request<Market.ListTradableResponseData>({
    url: "/market/exchange-info",
    method: "get",
    params: { market }
  })
}

/** 读取币安原生 K 线 */
export function listKlinesApi(params: Market.ListKlinesParamsRequestData) {
  return request<Market.ListKlinesResponseData>({
    url: "/market/klines",
    method: "get",
    params
  })
}

/** 读取币安 24 小时行情 */
export function getTickerApi(market: Market.Market, symbol: string) {
  return request<Market.GetTickerResponseData>({
    url: "/market/ticker",
    method: "get",
    params: { market, symbol }
  })
}

/** 使用一次性票据连接行情 WebSocket */
export function createMarketSocketApi(ticket: string) {
  return new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/market/ws`, ["alphapulse.market.v1", `ticket.${ticket}`])
}
