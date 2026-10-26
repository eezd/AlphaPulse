import type * as Auth from "./type"
import { request } from "@/http/axios"

/** 获取当前身份 */
export function getIdentityApi() {
  return request<Auth.GetIdentityResponseData>({
    url: "/auth/me",
    method: "get"
  })
}

/** 撤销当前会话 */
export function revokeSessionApi() {
  return request<Auth.RevokeSessionResponseData>({
    url: "/auth/logout",
    method: "post"
  })
}

/** 获取登录验证码模式及挑战 */
export function getCaptchaApi() {
  return request<Auth.GetCaptchaResponseData>({
    url: "/auth/captcha",
    method: "get",
    anonymous: true,
    notify: false
  })
}

/** 提交登录凭据 */
export function loginApi(body: Auth.LoginForm) {
  return request<Auth.LoginResponseData>({
    url: "/auth/login",
    method: "post",
    data: body,
    anonymous: true
  })
}

/** 一次性短票据，只用于行情 WebSocket 握手。 */
export function createWebSocketTicketApi() {
  return request<Auth.WebSocketTicket>({
    url: "/auth/ws-ticket",
    method: "post"
  })
}
