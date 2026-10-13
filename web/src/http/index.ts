import axios from "axios"
import { ElMessage } from "element-plus"
import { router } from "@/router"

// 鉴权靠 httpOnly cookie，同源请求自动携带
export const http = axios.create({ baseURL: "/api", timeout: 15000 })

http.interceptors.response.use(
  response => response.data,
  (error) => {
    const url = error.config?.url
    // /auth/me 的 401 由路由守卫处理；其余接口 401 说明登录过期
    if (error.response?.status === 401 && url !== "/auth/login") {
      if (url !== "/auth/me")
        router.replace({ name: "login", query: { redirect: router.currentRoute.value.fullPath } })
    } else {
      ElMessage.error(error.response?.data?.detail ?? error.message)
    }
    return Promise.reject(error)
  }
)
