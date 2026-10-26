import type { AxiosRequestConfig } from "axios"
import axios, { CanceledError } from "axios"
import { ElMessage } from "element-plus"
import { useUserStore } from "@/pinia/stores/user"
import { router } from "@/router"

declare module "axios" {
  interface AxiosRequestConfig {
    anonymous?: boolean
    sessionVersion?: number
    notify?: boolean
    /** 明确声明未包装的 JSON，例如 OpenAPI；不自动探测响应格式。 */
    rawResponse?: boolean
  }
}

export interface ApiEnvelope<T> {
  code: number
  data: T
  message: string
}

export class ApiError extends Error {
  constructor(public code: number, message: string) {
    super(message)
  }
}

export function readEnvelope<T>(value: unknown): ApiEnvelope<T> {
  if (!value || typeof value !== "object" || !("code" in value) || typeof value.code !== "number"
    || !("message" in value) || typeof value.message !== "string" || !("data" in value)) {
    throw new ApiError(502, "服务器响应格式无效")
  }
  return value as ApiEnvelope<T>
}

/** 无 Vue 生命周期接管的轮询回调：请求层已提示，保留真实错误供排查。 */
export function logRequestError(error: unknown) {
  if (!axios.isCancel(error)) console.error(error)
}

export function rejectAuthentication(version: number, redirect = true) {
  const user = useUserStore()
  if (version !== user.sessionVersion)
    return
  user.setToken("")
  const route = router.currentRoute.value
  if (redirect && route.name !== "login")
    void router.replace({ name: "login", query: { redirect: route.fullPath } })
}

const client = axios.create({ baseURL: "/api", timeout: 15000 })
client.interceptors.request.use((config) => {
  const user = useUserStore()
  if (config.sessionVersion !== user.sessionVersion || (!config.anonymous && !user.token))
    throw new CanceledError("会话已切换")
  if (!config.anonymous && user.token)
    config.headers.set("Authorization", `Bearer ${user.token}`)
  return config
})

export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const version = useUserStore().sessionVersion
  config.sessionVersion = version
  try {
    const response = await client.request(config)
    if (version !== useUserStore().sessionVersion)
      throw new CanceledError("会话已切换")
    if (config.rawResponse) return response.data
    const body = readEnvelope<T>(response.data)
    if (body.code !== 0)
      throw new ApiError(body.code, body.message)
    return body.data
  } catch (error) {
    if (version !== useUserStore().sessionVersion)
      throw new CanceledError("会话已切换")
    if (axios.isCancel(error))
      throw error
    let reason = error
    if (axios.isAxiosError(error) && error.response) {
      try {
        const body = readEnvelope(error.response.data)
        reason = new ApiError(error.response.status, body.message || `请求失败：HTTP ${error.response.status}`)
      } catch (invalid) {
        reason = new ApiError(error.response.status, invalid instanceof Error ? invalid.message : "服务器响应格式无效")
      }
    }
    if (reason instanceof ApiError && reason.code === 401 && !config.anonymous) {
      rejectAuthentication(version, config.url !== "/auth/me")
    } else if (config.notify !== false) {
      ElMessage.error(reason instanceof Error ? reason.message : "请求失败")
    }
    throw reason
  }
}
