import type * as Jin10 from "./type"
import { request } from "@/http/axios"

export function getJin10Api() {
  return request<Jin10.Jin10Status>({ url: "/settings/jin10", method: "get" })
}
export function createJin10QrApi() {
  return request<Jin10.Jin10Qr>({ url: "/settings/jin10/qr", method: "post" })
}
export function pollJin10QrApi(id: string) {
  return request<Jin10.Jin10Poll>({ url: `/settings/jin10/qr/${id}/poll`, method: "post" })
}
export function cancelJin10QrApi(id: string) {
  return request<null>({ url: `/settings/jin10/qr/${id}`, method: "delete", notify: false })
}
export function disconnectJin10Api() {
  return request<null>({ url: "/settings/jin10", method: "delete" })
}
