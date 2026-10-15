<script lang="ts" setup>
import { TrendCharts } from "@element-plus/icons-vue"
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
  <div class="h-full flex-center bg-slate-50">
    <el-card class="w-90" shadow="never" body-class="px-8 pb-8 pt-6">
      <div class="mb-6 text-center">
        <div class="flex-center gap-2 text-2xl text-slate-900 font-bold">
          <el-icon :size="26" color="var(--el-color-primary)">
            <TrendCharts />
          </el-icon>
          AlphaPulse
        </div>
        <div class="mt-1 text-xs text-slate-500">
          加密货币交易研究工具
        </div>
      </div>
      <el-form :model="form" @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" show-password autocomplete="current-password" />
        </el-form-item>
        <el-button type="primary" native-type="submit" size="large" :loading="loading" class="mt-2 w-full">
          登录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>
