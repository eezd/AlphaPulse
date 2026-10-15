import { ChatDotRound, Collection, DataAnalysis, Document, Monitor, Setting, TrendCharts } from "@element-plus/icons-vue"
import { createRouter, createWebHistory } from "vue-router"
import Layout from "@/layouts/index.vue"
import { useUserStore } from "@/pinia/user"

export const menuRoutes = [
  { path: "/news", name: "news", component: () => import("@/pages/news/index.vue"), meta: { title: "新闻流", icon: Document } },
  { path: "/crawler", name: "crawler", component: () => import("@/pages/crawler/index.vue"), meta: { title: "采集控制台", icon: Monitor } },
  { path: "/market", name: "market", component: () => import("@/pages/market/index.vue"), meta: { title: "行情", icon: TrendCharts } },
  { path: "/trading", name: "trading", component: () => import("@/pages/trading/index.vue"), meta: { title: "模拟盘", icon: DataAnalysis } },
  { path: "/chat", name: "chat", component: () => import("@/pages/chat/index.vue"), meta: { title: "Chat", icon: ChatDotRound } },
  { path: "/skills", name: "skills", component: () => import("@/pages/skills/index.vue"), meta: { title: "Skills", icon: Collection } },
  { path: "/settings", name: "settings", component: () => import("@/pages/settings/index.vue"), meta: { title: "环境变量", icon: Setting } }
]

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: () => import("@/pages/login/index.vue") },
    { path: "/", component: Layout, redirect: "/news", children: menuRoutes },
    { path: "/:pathMatch(.*)*", redirect: "/" }
  ]
})

router.beforeEach(async (to) => {
  if (to.name === "login")
    return true
  const userStore = useUserStore()
  if (!userStore.username) {
    try {
      await userStore.fetchMe()
    } catch {
      return { name: "login", query: { redirect: to.fullPath } }
    }
  }
  return true
})
