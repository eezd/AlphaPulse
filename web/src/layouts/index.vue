<script lang="ts" setup>
import { DataLine } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/user"
import { menuRoutes } from "@/router"

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

async function logout() {
  await userStore.logout()
  router.replace({ name: "login" })
}
</script>

<template>
  <div class="h-full flex bg-ap-bg">
    <aside class="w-[200px] flex shrink-0 flex-col border-r border-r-ap-line border-r-solid bg-ap-panel">
      <div class="h-14 flex shrink-0 flex-col justify-center border-b border-b-ap-line border-b-solid bg-ap-panel-high px-3">
        <div class="flex items-center gap-2 font-head text-base text-ap-brand font-bold tracking-wider">
          <el-icon :size="18">
            <DataLine />
          </el-icon>
          ALPHAPULSE
        </div>
        <div class="mt-0.5 text-[10px] text-ap-muted tracking-[0.2em]">
          TRADING TERMINAL
        </div>
      </div>
      <nav class="flex flex-col py-1">
        <router-link v-for="item in menuRoutes" :key="item.path" :to="item.path" class="nav-item" active-class="active">
          <el-icon :size="16">
            <component :is="item.meta.icon" />
          </el-icon>
          {{ item.meta.title }}
        </router-link>
      </nav>
    </aside>
    <div class="min-w-0 flex flex-1 flex-col">
      <header class="h-14 flex shrink-0 items-center justify-between border-b border-b-ap-line border-b-solid bg-ap-panel px-4">
        <div class="flex items-center gap-2">
          <span class="font-head text-base text-ap-brand font-bold">AlphaPulse</span>
          <span class="text-ap-line">/</span>
          <span class="font-bold tracking-wide">{{ route.meta.title }}</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="h-6 w-6 flex-center border border-ap-line border-solid bg-ap-panel-highest text-[11px] text-ap-brand font-bold">
            {{ userStore.username!.slice(0, 2).toUpperCase() }}
          </span>
          <span class="font-semibold">{{ userStore.username }}</span>
          <el-button link @click="logout">
            退出
          </el-button>
        </div>
      </header>
      <main class="min-h-0 flex-1 overflow-auto p-2">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-left: 2px solid transparent;
  color: var(--ap-muted);
  font-weight: 600;
  text-decoration: none;
}
.nav-item:hover {
  background: var(--ap-panel-highest);
  color: var(--ap-text);
}
.nav-item.active {
  border-left-color: var(--ap-brand);
  background: var(--ap-panel-high);
  color: var(--ap-brand);
}
</style>
