<script lang="ts" setup>
import type { FormInstance, FormRules } from "element-plus"
import { getCaptchaApi, loginApi } from "@@/apis/auth"
import axios, { CanceledError } from "axios"
import { useUserStore } from "@/pinia/stores/user"
import { safeRedirect } from "@/router/guards"

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const rememberName = ref(!!localStorage.getItem("alphapulse.username"))
const form = reactive({ username: localStorage.getItem("alphapulse.username") ?? "", password: "", captcha_id: "", code: "" })
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
    if (rememberName.value) localStorage.setItem("alphapulse.username", form.username)
    else localStorage.removeItem("alphapulse.username")
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
  <div class="login-terminal">
    <header><span class="brand-mark">α</span><strong>AlphaPulse</strong><small>RESEARCH TERMINAL</small></header><div class="login-content">
      <section class="login-intro">
        <span class="text-blue-600 text-xs">新闻 · 事件 · 市场</span><h1>机构级决策与量化研究工作台</h1><p>从原始信息到可追溯的事件判断，连接新闻、市场与研究。</p><div class="login-features">
          <div><b>全球新闻采集</b><p>官方来源与实时新闻</p></div><div><b>事件持续研究</b><p>关键词、证据与主体时间线</p></div><div><b>实时行情</b><p>价格、盘口与 K 线分析</p></div>
        </div>
      </section>
      <el-card class="w-90" shadow="never" body-class="px-8 pb-8 pt-6">
        <div class="mb-6 text-center">
          <div class="flex-center gap-2 text-2xl text-slate-900 font-bold">
            <img src="/favicon.svg" alt="" aria-hidden="true" width="32" height="32" class="shrink-0">
            AlphaPulse
          </div>
          <div class="mt-1 text-xs text-slate-500">
            登录研究工作台
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
          <el-checkbox v-model="rememberName">
            记住用户名
          </el-checkbox>
          <el-button type="primary" native-type="submit" size="large" :loading="loading || refreshing" :disabled="!captchaReady" class="mt-2 w-full">
            登录
          </el-button>
        </el-form>
      </el-card>
    </div><footer>AlphaPulse · 新闻与金融研究工作台 · 仅限授权访问</footer>
  </div>
</template>

<style scoped>
.login-terminal {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: radial-gradient(ellipse at 15% 20%, #e9efff, transparent 65%), #f8fafc;
}
header {
  height: 52px;
  padding: 0 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  background: white;
  border-bottom: 1px solid #e2e8f0;
  color: #2563eb;
}
header small {
  font-size: 10px;
  color: #64748b;
}
.login-content {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 100px;
  padding: 24px;
}
.login-intro {
  max-width: 560px;
}
.login-intro h1 {
  font-size: 27px;
  letter-spacing: -0.04em;
}
.login-intro p {
  font-size: 13px;
  color: #64748b;
  line-height: 1.8;
}
.login-features {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-top: 26px;
}
.login-features > div {
  border: 1px solid #dbe4f4;
  padding: 15px;
  background: #ffffff90;
  border-radius: 5px;
  font-size: 12px;
}
.login-features p {
  font-size: 11px;
}
footer {
  padding: 10px 24px;
  background: white;
  border-top: 1px solid #e2e8f0;
  font-size: 10px;
  color: #64748b;
}
@media (max-width: 1000px) {
  .login-content {
    gap: 30px;
  }
  .login-intro {
    max-width: 400px;
  }
}
@media (max-width: 760px) {
  .login-intro {
    display: none;
  }
  .login-content {
    padding: 12px;
  }
  .login-content .el-card {
    width: min(100%, 380px);
  }
}
</style>
