<script lang="ts" setup>
import { SwitchButton, TrendCharts } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/user"
import { menuRoutes } from "@/router"

const router = useRouter()
const userStore = useUserStore()

async function logout() {
  await userStore.logout()
  router.replace({ name: "login" })
}
</script>

<template>
  <div class="h-full flex flex-col">
    <header class="h-14 flex shrink-0 items-center gap-10 border-b border-b-slate-200 border-b-solid bg-white px-6 shadow-sm">
      <div class="flex items-center gap-2 text-lg text-slate-900 font-bold">
        <el-icon :size="22" color="var(--el-color-primary)">
          <TrendCharts />
        </el-icon>
        AlphaPulse
      </div>
      <nav class="h-full flex gap-7 text-sm">
        <RouterLink v-for="item in menuRoutes" :key="item.path" :to="item.path" class="nav-link" active-class="active">
          {{ item.meta.title }}
        </RouterLink>
      </nav>
      <div class="flex-1" />
      <div class="flex items-center gap-2 text-sm text-slate-700">
        <span class="h-8 w-8 flex-center border border-sky-200 rounded-full border-solid bg-sky-50 text-xs text-[var(--el-color-primary)] font-bold">
          {{ userStore.username?.slice(0, 2).toUpperCase() }}
        </span>
        {{ userStore.username }}
      </div>
      <el-button link :icon="SwitchButton" @click="logout">
        退出登录
      </el-button>
    </header>
    <main class="min-h-0 flex-1 overflow-auto p-4">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
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
