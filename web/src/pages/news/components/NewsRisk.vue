<script setup lang="ts">
import type { Analysis, RiskEvent } from "@/common/apis/risk/type"
import { listNewsSummariesApi, listRiskAnalysesApi } from "@/common/apis/risk"
import { useUserStore } from "@/pinia/stores/user"

const props = defineProps<{ newsId: number, revision: number }>()
const user = useUserStore()
const analysis = ref<Analysis | null>(null)
const summaries = ref<RiskEvent[]>([])
const failed = ref(false)
const loading = ref(false)
const ownerVersion = user.sessionVersion
let sequence = 0
let active = true
let timer: ReturnType<typeof setInterval> | undefined
async function load(reset = true) {
  if (!active || ownerVersion !== user.sessionVersion) return
  const seq = ++sequence
  const version = user.sessionVersion
  loading.value = true
  if (reset) {
    analysis.value = null
    summaries.value = []
    failed.value = false
  }
  try {
    const [rows, events] = await Promise.all([listRiskAnalysesApi({ news_id: props.newsId, revision_no: props.revision }, reset), listNewsSummariesApi(props.newsId, props.revision)])
    if (seq === sequence && version === user.sessionVersion) {
      summaries.value = events
      analysis.value = rows[0] ?? null
      failed.value = false
    }
  } catch {
    if (seq === sequence && version === user.sessionVersion) failed.value = true
  } finally {
    if (seq === sequence && version === user.sessionVersion) loading.value = false
  }
}
watch(() => [props.newsId, props.revision], () => void load(), { immediate: true })
onMounted(() => {
  timer = setInterval(() => {
    if (!document.hidden && !loading.value && (!["done"].includes(analysis.value?.state ?? "") || summaries.value.some(e => ["pending", "processing"].includes(e.state)))) void load(false)
  }, 15000)
})
onBeforeUnmount(() => {
  active = false
  sequence++
  clearInterval(timer)
})
</script>

<template>
  <div class="rounded bg-sky-50 p-3 text-sm">
    <b>AI 关联新闻包</b>
    <div v-for="event in summaries" :key="event.id" class="my-2">
      <router-link :to="{ path: '/home', query: { summary: event.id } }">
        {{ event.title }}
      </router-link>
      <p>{{ event.result?.summary || '事件归并完成，等待二阶段总结' }}</p>
      <p v-if="event.error">
        更新失败：{{ event.error }}
      </p>
    </div>
    <p v-if="!summaries.length">
      {{ failed ? '关联事件读取失败' : analysis?.state === 'failed' ? `初筛失败：${analysis.error}` : '暂无关联总结；新流程只处理启用后的新闻' }}
      <el-button v-if="failed" link :loading="loading" @click="load()">
        重试
      </el-button>
    </p>
  </div>
</template>
