import type { Router } from "vue-router"
import axios from "axios"
import { ApiError } from "@/http/axios"
import { useUserStore } from "@/pinia/stores/user"

export function safeRedirect(value: unknown): string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    && !value.includes("\\") && !/\s/.test(value) && !value.startsWith("/login")
    ? value
    : "/home"
}

export function installGuards(router: Router) {
  router.beforeEach(async (to) => {
    const user = useUserStore()
    const version = user.sessionVersion
    if (to.meta.public && to.name !== "login")
      return true
    if (!user.token)
      return to.name === "login" ? true : { name: "login", query: { redirect: to.fullPath } }
    try {
      if (!user.identity)
        await user.fetchMe()
    } catch (error) {
      if (error instanceof ApiError && error.code === 401 && !user.token && user.sessionVersion === version + 1)
        return { name: "login", query: { redirect: to.fullPath } }
      if (version !== user.sessionVersion || axios.isCancel(error))
        return false
      if (error instanceof ApiError && error.code === 401)
        return { name: "login", query: { redirect: to.fullPath } }
      return { name: "connection-error", query: { redirect: to.fullPath } }
    }
    if (version !== user.sessionVersion)
      return false
    if (to.name === "login")
      return safeRedirect(to.query.redirect)
    if (to.meta.admin && !user.identity?.is_admin)
      return { name: "forbidden" }
    return true
  })
}
