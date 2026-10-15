<script lang="ts" setup>
import { DataLine } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/user"

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const form = reactive({ username: "", password: "" })
const loading = ref(false)

async function submit() {
  loading.value = true
  try {
    await userStore.login(form.username, form.password)
    router.replace((route.query.redirect as string) || "/")
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-bg h-full flex-center">
    <div class="w-90 border border-ap-line border-solid bg-ap-panel">
      <div class="h-14 flex flex-col justify-center border-b border-b-ap-line border-b-solid bg-ap-panel-high px-4">
        <div class="flex items-center gap-2 font-head text-lg text-ap-brand font-bold tracking-wider">
          <el-icon :size="20">
            <DataLine />
          </el-icon>
          ALPHAPULSE
        </div>
        <div class="text-[10px] text-ap-muted tracking-[0.2em]">
          新闻 · 行情 · AI 分析 · 模拟盘
        </div>
      </div>
      <el-form :model="form" class="p-5" @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" show-password autocomplete="current-password" />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="w-full">
          登录
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-bg {
  background-color: var(--ap-bg);
  background-image:
    linear-gradient(color-mix(in srgb, var(--ap-line) 25%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--ap-line) 25%, transparent) 1px, transparent 1px);
  background-size: 32px 32px;
}
</style>
