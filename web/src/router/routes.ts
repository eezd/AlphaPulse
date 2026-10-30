export const menuRoutes = [
  { path: "/home", name: "home", component: () => import("@/pages/home/index.vue"), meta: { admin: true, title: "首页" } },
  { path: "/news", name: "news", component: () => import("@/pages/news/index.vue"), meta: { admin: true, title: "新闻流" } },
  { path: "/calendar", name: "calendar", component: () => import("@/pages/calendar/index.vue"), meta: { admin: true, title: "日历" } },
  { path: "/crawler", name: "crawler", component: () => import("@/pages/crawler/index.vue"), meta: { admin: true, title: "采集控制台" } },
  { path: "/market", name: "market", component: () => import("@/pages/market/index.vue"), meta: { admin: true, title: "行情" } },
  { path: "/trading", name: "trading", component: () => import("@/pages/trading/index.vue"), meta: { admin: true, title: "模拟盘" } },
  { path: "/chat", name: "chat", component: () => import("@/pages/chat/index.vue"), meta: { admin: true, title: "Chat" } },
  { path: "/skills", name: "skills", component: () => import("@/pages/skills/index.vue"), meta: { admin: true, title: "Skills" } },
  { path: "/settings", name: "settings", component: () => import("@/pages/settings/index.vue"), meta: { admin: true, title: "环境变量" } }
]
