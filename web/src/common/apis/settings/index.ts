import type * as Settings from "./type"
import { request } from "@/http/axios"

/** 读取模型配置（不返回密钥） */
export function listProvidersApi() {
  return request<Settings.ListProvidersResponseData>({
    url: "/settings/providers",
    method: "get"
  })
}

/** Read the selected provider's catalog; the page owns failure presentation. */
export function listProviderModelsApi(id: number) {
  return request<Settings.ListProviderModelsResponseData>({
    url: `/settings/providers/${id}/models`,
    method: "get",
    timeout: 35000,
    notify: false
  })
}

/** 新增模型配置 */
export function createProviderApi(body: Settings.ProviderForm) {
  return request<Settings.CreateProviderResponseData>({
    url: "/settings/providers",
    method: "post",
    data: body
  })
}

/** 修改模型配置；空密钥保持原值 */
export function patchProviderApi(id: number, body: Partial<Settings.ProviderForm>) {
  return request<Settings.PatchProviderResponseData>({
    url: `/settings/providers/${id}`,
    method: "patch",
    data: body
  })
}

/** 删除模型配置 */
export function deleteProviderApi(id: number) {
  return request<Settings.DeleteProviderResponseData>({
    url: `/settings/providers/${id}`,
    method: "delete"
  })
}

/** 测试模型连接；按后端 SDK 重试上限等待 */
export function testProviderApi(id: number) {
  return request<Settings.TestProviderResponseData>({
    url: `/settings/providers/${id}/test`,
    method: "post",
    data: null,
    timeout: 1900000
  })
}

/** 读取当前模拟盘交易所设置 */
export function getExchangeApi() {
  return request<Settings.GetExchangeResponseData>({
    url: "/settings/exchange",
    method: "get"
  })
}

/** 切换交易所或保存其凭证 */
export function putExchangeApi(body: Settings.PutExchangeBodyRequestData) {
  return request<Settings.PutExchangeResponseData>({
    url: "/settings/exchange",
    method: "put",
    data: body
  })
}

/** 删除当前交易所凭证 */
export function deleteExchangeApi() {
  return request<Settings.DeleteExchangeResponseData>({
    url: "/settings/exchange",
    method: "delete"
  })
}

/** 测试当前模拟盘连接 */
export function testExchangeApi() {
  return request<Settings.TestExchangeResponseData>({
    url: "/settings/exchange/test",
    method: "post",
    data: null,
    timeout: 30000
  })
}
