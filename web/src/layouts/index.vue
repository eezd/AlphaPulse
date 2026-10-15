<script lang="ts" setup>
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
  <el-container class="h-full">
    <el-aside width="200px" class="border-r border-r-solid border-[var(--el-border-color)]">
      <div class="h-14 flex-center text-lg font-bold">
        AlphaPulse
      </div>
      <el-menu :default-active="route.path" router class="border-r-0!">
        <el-menu-item v-for="item in menuRoutes" :key="item.path" :index="item.path">
          {{ item.meta.title }}
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="flex-y-center justify-end gap-3 border-b border-b-solid border-[var(--el-border-color)]">
        <span>{{ userStore.username }}</span>
        <el-button link @click="logout">
          退出
        </el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
