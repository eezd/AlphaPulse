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

/** 测试模型连接；按单请求重试上限等待 */
export function testProviderApi(id: number) {
  return request<Settings.TestProviderResponseData>({
    url: `/settings/providers/${id}/test`,
    method: "post",
    data: null,
    timeout: 2100000
  })
}

export interface ProviderAvailability { provider_id: number, name: string, state: string, retry_at: string | null, level: number }
export const listProviderAvailabilityApi = () => request<ProviderAvailability[]>({ url: "/settings/provider-availability", method: "get", notify: false })
