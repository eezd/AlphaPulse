import type * as Docs from "./type"
import { request } from "@/http/axios"

/** OpenAPI 按原始规范返回，不使用普通 JSON 外壳。 */
export function getOpenApiApi(signal?: AbortSignal) {
  return request<Docs.OpenApiDocument>({
    url: "/openapi.json",
    method: "get",
    rawResponse: true,
    notify: false,
    signal
  })
}
