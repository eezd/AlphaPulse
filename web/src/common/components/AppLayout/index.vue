<script lang="ts" setup>
import { SwitchButton } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/stores/user"
import { menuRoutes } from "@/router/routes"

const router = useRouter()
const userStore = useUserStore()

async function logout() {
  await userStore.logout()
  router.replace({ name: "login" })
}
</script>

<template>
  <div class="h-full flex flex-col">
    <header class="app-header h-auto flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0 border-b border-b-slate-200 border-b-solid bg-white px-3 pt-2 shadow-sm lg:h-14 lg:flex-nowrap lg:gap-6 lg:px-6 lg:pt-0">
      <div class="brand flex flex-1 shrink-0 items-center gap-2 text-base lg:flex-none lg:text-lg text-slate-900 font-bold">
        <img src="/favicon.svg" alt="" aria-hidden="true" width="24" height="24" class="shrink-0">
        AlphaPulse
      </div>
      <nav class="app-nav order-4 h-10 min-w-0 flex flex-1 basis-full gap-5 overflow-x-auto text-sm lg:order-none lg:h-full lg:basis-auto lg:gap-6">
        <RouterLink v-for="item in menuRoutes" :key="item.path" :to="item.path" class="nav-link" active-class="active">
          {{ item.meta.title }}
        </RouterLink>
      </nav>
      <div class="user-label shrink-0 hidden items-center lg:flex gap-2 text-sm text-slate-700">
        <span class="h-8 w-8 flex-center border border-sky-200 rounded-full border-solid bg-sky-50 text-xs text-[var(--el-color-primary)] font-bold">
          {{ userStore.username?.slice(0, 2).toUpperCase() }}
        </span>
        {{ userStore.username }}
      </div>
      <el-button link :icon="SwitchButton" @click="logout">
        退出登录
      </el-button>
    </header>
    <main class="app-main min-h-0 min-w-0 flex-1 overflow-auto p-2 lg:p-3">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.app-nav {
  scrollbar-width: none;
}
.nav-link {
  white-space: nowrap;
  flex-shrink: 0;
}

.nav-link {
  display: flex;
  align-items: center;
  border-bottom: 2px solid transparent;
  color: var(--el-text-color-secondary);
  text-decoration: none;
}
.nav-link:hover {
  color: var(--el-text-color-primary);
}
.nav-link.active {
  border-bottom-color: var(--el-color-primary);
  color: var(--el-color-primary);
  font-weight: 600;
}
</style>
