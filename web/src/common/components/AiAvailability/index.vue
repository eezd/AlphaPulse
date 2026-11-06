<script setup lang="ts">
import type { ProviderAvailability } from "@@/apis/settings"
import { listProviderAvailabilityApi } from "@@/apis/settings"
import { useSessionScope } from "@@/composables/useSessionScope"

const scope = useSessionScope()
const states = ref<ProviderAvailability[]>([])
const failed = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let closed = false
async function refresh() {
  try {
    const rows = await listProviderAvailabilityApi()
    if (closed) return
    scope.assertCurrent()
    states.value = rows.filter(row => row.state !== "normal")
    failed.value = false
  } catch (error) {
    if (!closed) {
      failed.value = true
      console.error(error)
    }
  } finally {
    if (!closed) timer = setTimeout(refresh, 5000)
  }
}
onMounted(refresh)
onBeforeUnmount(() => {
  closed = true
  clearTimeout(timer)
})
</script>

<template>
  <el-alert v-if="failed" class="mb-3" type="warning" :closable="false" title="模型接入状态读取失败" />
  <el-alert v-for="row in states" :key="row.provider_id" class="mb-3" type="warning" :closable="false">
    {{ row.name }}：{{ row.state === 'probing' ? '正在单请求试探恢复' : '接入暂时不可用，等待恢复' }}。
    下次尝试 {{ row.retry_at ? new Date(row.retry_at).toLocaleString() : '—' }}；等待期间保留任务。
  </el-alert>
</template>
