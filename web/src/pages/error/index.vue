<script setup lang="ts">
import { useUserStore } from "@/pinia/stores/user"
import { safeRedirect } from "@/router/guards"

const user = useUserStore()
const route = useRoute()
const router = useRouter()
const status = computed(() => Number(route.meta.status))

async function logout() {
  await user.logout()
  await router.replace({ name: "login" })
}
</script>

<template>
  <div class="h-full flex-center flex-col gap-4">
    <h1>{{ status }}</h1>
    <p>{{ status === 403 ? '当前账号没有访问权限' : status === 404 ? '页面不存在' : '暂时无法恢复身份，请重试' }}</p>
    <el-button @click="router.replace(status === 503 ? safeRedirect(route.query.redirect) : '/news')">
      {{ status === 503 ? '重试' : '返回首页' }}
    </el-button>
    <el-button v-if="user.token" link @click="logout">
      退出登录
    </el-button>
  </div>
</template>
