<script setup lang="ts">
import type { Jin10Qr, Jin10Status } from "@@/apis/jin10/type"
import { cancelJin10QrApi, createJin10QrApi, disconnectJin10Api, getJin10Api, pollJin10QrApi } from "@@/apis/jin10"
import { useSessionScope } from "@@/composables/useSessionScope"
import { useUserStore } from "@/pinia/stores/user"

const scope = useSessionScope()
const user = useUserStore()
const sessionVersion = user.sessionVersion
const sameSession = () => sessionVersion === user.sessionVersion
const status = ref<Jin10Status>()
const qr = ref<Jin10Qr>()
const visible = ref(false)
const busy = ref(false)
const state = ref("idle")
const failed = ref(false)
const syncLabels = { awaiting_validation: "同步尚未就绪，账号连接不代表日历已完整收录", disconnected: "尚未连接", connecting: "正在验证日历连接", syncing: "正在同步日历", ready: "日历连接已验证，持续同步中", error: "日历同步失败，请查看下方原因" }
let generation = 0
let statusRequest = 0
let timer: ReturnType<typeof setTimeout> | undefined

async function load() {
  const request = ++statusRequest
  const current = generation
  try {
    const result = await getJin10Api()
    if (scope.isCurrent() && request === statusRequest && current === generation) {
      status.value = result
      failed.value = false
    }
  } catch {
    if (scope.isCurrent() && request === statusRequest && current === generation) failed.value = true
  }
}
function stop() {
  ++generation
  clearTimeout(timer)
  const id = qr.value?.task_id
  qr.value = undefined
  if (id && sameSession()) void cancelJin10QrApi(id).catch(() => {})
}
async function poll(id: string, current: number) {
  if (!scope.isCurrent() || current !== generation || !visible.value) return
  try {
    const result = await pollJin10QrApi(id)
    if (!scope.isCurrent() || current !== generation || !visible.value) return
    state.value = result.state
    if (result.state === "pending") {
      timer = setTimeout(poll, 5000, id, current)
    } else {
      qr.value = undefined
      if (result.state === "connected") {
        visible.value = false
        const completed = generation
        await load()
        if (scope.isCurrent() && completed === generation) ElMessage.success("金十账号已连接，日历读取权限待核验")
      }
    }
  } catch {
    if (scope.isCurrent() && current === generation) state.value = "error"
  }
}
async function start() {
  if (busy.value) return
  stop()
  const current = generation
  visible.value = true
  busy.value = true
  state.value = "loading"
  try {
    const result = await createJin10QrApi()
    if (!scope.isCurrent() || current !== generation || !visible.value) {
      if (sameSession()) await cancelJin10QrApi(result.task_id)
      return
    }
    qr.value = result
    state.value = "pending"
    timer = setTimeout(poll, 5000, result.task_id, current)
  } catch {
    if (scope.isCurrent() && current === generation) state.value = "error"
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
async function disconnect() {
  if (busy.value) return
  const confirmed = generation
  await ElMessageBox.confirm("断开本项目的金十连接？已保存的日历历史会保留。", "断开连接")
  scope.assertCurrent()
  if (confirmed !== generation || busy.value) return
  stop()
  const current = generation
  busy.value = true
  try {
    await disconnectJin10Api()
    if (scope.isCurrent() && current === generation) await load()
  } finally {
    if (scope.isCurrent() && current === generation) busy.value = false
  }
}
onMounted(load)
onBeforeUnmount(stop)
watch(visible, (value) => {
  if (!value) stop()
}, { flush: "sync" })
</script>

<template>
  <el-card shadow="never">
    <template #header>
      金十日历
    </template>
    <el-alert v-if="failed" title="连接状态读取失败" type="error" :closable="false" />
    <template v-else-if="status">
      <p>
        <el-tag :type="status.connected ? 'success' : 'info'">
          {{ status.connected ? "账号已连接" : "尚未连接" }}
        </el-tag>
      </p>
      <p>全球宏观数据、重要事件和假期。官方同口径数据优先，金十补充缺项。</p>
      <p v-if="status.connected">
        {{ syncLabels[status.sync_state] }}
      </p>
      <p v-if="status.last_success_at">
        最近成功：{{ status.last_success_at }}
      </p>
      <p v-if="status.history_next">
        历史回补下一日期：{{ status.history_next }}（北京时间，2025-01-01 起）
      </p>
      <el-alert v-if="status.error" :title="status.error" type="warning" :closable="false" />
      <el-button type="primary" :loading="busy" @click="start">
        {{ status.connected ? "重新扫码" : "扫码连接金十" }}
      </el-button>
      <el-button v-if="status.connected" @click="disconnect">
        断开连接
      </el-button>
      <el-button @click="load">
        刷新状态
      </el-button>
    </template>
    <el-button v-if="failed" @click="load">
      重试
    </el-button>
    <el-dialog v-model="visible" title="使用金十 App 扫码登录" width="min(420px, 95vw)" :close-on-click-modal="false">
      <div v-loading="busy" class="jin10-qr">
        <img v-if="qr && state === 'pending'" :src="qr.image" alt="金十登录二维码" width="240" height="240">
        <p v-if="state === 'pending'">
          请立即用金十 App 扫描并确认，二维码有效时间较短，失效后可刷新。
        </p>
        <p v-else-if="state === 'expired'">
          二维码已失效，请刷新后重新扫码。
        </p>
        <p v-else-if="state === 'error'">
          连接失败，请稍后重试。
        </p>
        <el-button v-if="state === 'expired' || state === 'error'" @click="start">
          刷新二维码
        </el-button>
      </div>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.jin10-qr {
  min-height: 260px;
  text-align: center;
}
.jin10-qr img {
  max-width: 100%;
  height: auto;
}
</style>
