<script lang="ts" setup>
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
  <div class="h-full flex-center">
    <el-card class="w-90">
      <template #header>
        <div class="text-center text-lg font-bold">
          AlphaPulse
        </div>
      </template>
      <el-form :model="form" @submit.prevent="submit">
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
    </el-card>
  </div>
</template>
