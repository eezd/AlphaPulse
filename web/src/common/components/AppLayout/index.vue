<script setup lang="ts">
import { Aim, Calendar, ChatDotRound, Collection, DataAnalysis, Document, Monitor, Setting, SwitchButton } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/stores/user"
import { menuRoutes } from "@/router/routes"

const router = useRouter()
const route = useRoute()
const user = useUserStore()
const icons = [DataAnalysis, Document, Calendar, Monitor, Aim, DataAnalysis, ChatDotRound, Collection, Setting]
const clock = ref("")
let timer: ReturnType<typeof setInterval>
onMounted(() => {
  const tick = () => clock.value = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date())
  tick()
  timer = setInterval(tick, 1000)
})
onBeforeUnmount(() => clearInterval(timer))
async function logout() {
  await user.logout()
  router.replace({ name: "login" })
}
function focusSearch(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    const input = document.querySelector<HTMLInputElement>("main input[placeholder*='搜索'], main input[placeholder*='关键']")
    if (input) {
      event.preventDefault()
      input.focus()
    }
  }
}
onMounted(() => window.addEventListener("keydown", focusSearch))
onBeforeUnmount(() => window.removeEventListener("keydown", focusSearch))
</script>

<template>
  <div class="terminal-shell">
    <header class="terminal-header">
      <RouterLink to="/home" class="terminal-brand">
        <span class="brand-mark">α</span> AlphaPulse <small>TERMINAL</small>
      </RouterLink>
      <nav class="terminal-tabs">
        <RouterLink v-for="item in menuRoutes" :key="item.path" :to="item.path" active-class="active">
          {{ item.name === 'home' ? '事件研究' : item.meta.title }}
        </RouterLink>
      </nav>
      <div class="terminal-account">
        <span class="terminal-clock">{{ clock }} 北京时间</span><RouterLink to="/settings">
          系统设置
        </RouterLink><el-button link :icon="SwitchButton" @click="logout">
          退出
        </el-button><span class="account-avatar" :title="user.username">{{ user.username?.slice(0, 2).toUpperCase() }}</span>
      </div>
    </header>
    <div class="terminal-body">
      <aside class="terminal-rail">
        <RouterLink v-for="(item, i) in menuRoutes" :key="item.path" :to="item.path" :title="item.name === 'home' ? '新闻与事件研究' : item.meta.title" active-class="active">
          <el-icon><component :is="icons[i]" /></el-icon>
        </RouterLink><el-popover title="快捷键" trigger="click" width="220">
          <template #reference>
            <el-button link class="rail-help" aria-label="快捷键帮助">
              ?
            </el-button>
          </template><p>Ctrl / ⌘ + K：当前页搜索</p><p>Ctrl + Enter：发送 Chat</p><p>Ctrl / ⌘ + S：保存 Skill</p>
        </el-popover>
      </aside>
      <main class="terminal-main" :class="`workspace-${String(route.name)}`">
        <router-view />
      </main>
    </div>
    <footer class="terminal-footer">
      <span><i /> AlphaPulse · {{ route.name === 'home' ? '新闻与事件研究' : route.meta.title }}</span><span>原始信息 · 证据核验 · 持续研究</span>
    </footer>
  </div>
</template>
