<script lang="ts" setup>
import type { FormInstance, FormRules } from "element-plus"
import { getCaptchaApi, loginApi } from "@@/apis/auth"
import { TrendCharts } from "@element-plus/icons-vue"
import axios, { CanceledError } from "axios"
import { useUserStore } from "@/pinia/stores/user"
import { safeRedirect } from "@/router/guards"

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const form = reactive({ username: "", password: "", captcha_id: "", code: "" })
const formRef = ref<FormInstance>()
const rules: FormRules = {
  username: [{ required: true, whitespace: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
  code: [{ validator: (_rule, value, callback) => {
    callback(captchaEnabled.value && !value?.trim() ? new Error("请输入验证码") : undefined)
  }, trigger: "blur" }]
}
const captchaError = ref("")
const captchaReady = ref(false)
const image = ref("")
const captchaEnabled = ref(false)
const refreshing = ref(false)
let challengeVersion = 0
let disposed = false

async function refreshCaptcha() {
  const version = ++challengeVersion
  refreshing.value = true
  captchaReady.value = false
  captchaError.value = ""
  form.captcha_id = ""
  form.code = ""
  try {
    const result = await getCaptchaApi()
    if (!disposed && version === challengeVersion) {
      captchaReady.value = true
      captchaEnabled.value = result.enabled
      if (result.enabled) {
        form.captcha_id = result.captcha_id
        image.value = result.image
      } else {
        image.value = ""
      }
    }
  } catch (error) {
    if (!disposed && version === challengeVersion)
      captchaError.value = error instanceof Error ? error.message : "无法读取登录验证配置"
    throw error
  } finally {
    if (version === challengeVersion) refreshing.value = false
  }
}
onMounted(refreshCaptcha)
onBeforeUnmount(() => {
  disposed = true
  challengeVersion++
})
const loading = ref(false)

async function submit() {
  if (loading.value || refreshing.value)
    return
  if (!captchaReady.value) return
  if (captchaEnabled.value && !form.captcha_id) return
  loading.value = true
  let attempted = false
  const version = userStore.sessionVersion
  try {
    await formRef.value!.validate()
    if (disposed || version !== userStore.sessionVersion) throw new CanceledError("页面或会话已切换")
    attempted = true
    const result = await loginApi({
      username: form.username,
      password: form.password,
      ...(captchaEnabled.value ? { captcha_id: form.captcha_id, code: form.code } : {})
    })
    if (disposed || version !== userStore.sessionVersion) throw new CanceledError("页面或会话已切换")
    userStore.setToken(result.token)
    await router.replace(safeRedirect(route.query.redirect))
  } catch (error) {
    if (attempted && captchaEnabled.value && !disposed && !axios.isCancel(error))
      await Promise.allSettled([refreshCaptcha()])
    throw error
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
      <el-alert v-if="captchaError" type="error" :closable="false" class="mb-4" :title="captchaError">
        <el-button link type="primary" :loading="refreshing" @click="refreshCaptcha">
          重试
        </el-button>
      </el-alert>
      <el-form ref="formRef" :model="form" :rules="rules" :disabled="loading" @submit.prevent="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" placeholder="密码" show-password autocomplete="current-password" />
        </el-form-item>
        <el-form-item v-if="captchaEnabled" prop="code">
          <div class="w-full flex gap-2">
            <el-input v-model="form.code" placeholder="验证码" autocomplete="off" maxlength="5" :disabled="loading" />
            <button type="button" class="shrink-0 cursor-pointer" :disabled="loading || refreshing" title="刷新验证码" @click="refreshCaptcha">
              <img v-if="image" :src="image" alt="登录验证码，点击刷新" class="h-10 w-30">
              <span v-else>刷新验证码</span>
            </button>
          </div>
        </el-form-item>
        <el-button type="primary" native-type="submit" size="large" :loading="loading || refreshing" :disabled="!captchaReady" class="mt-2 w-full">
          登录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>
