import Layout from "@@/components/AppLayout/index.vue"
import { createRouter, createWebHistory } from "vue-router"
import { installGuards } from "./guards"

import { menuRoutes } from "./routes"

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: () => import("@/pages/login/index.vue") },
    { path: "/", component: Layout, redirect: "/home", children: menuRoutes },
    { path: "/docs", name: "docs", component: () => import("@/pages/docs/index.vue"), meta: { admin: true } },
    { path: "/403", name: "forbidden", component: () => import("@/pages/error/index.vue"), meta: { status: 403 } },
    { path: "/connection-error", name: "connection-error", component: () => import("@/pages/error/index.vue"), meta: { public: true, status: 503 } },
    { path: "/:pathMatch(.*)*", name: "not-found", component: () => import("@/pages/error/index.vue"), meta: { status: 404 } }
  ]
})

installGuards(router)
