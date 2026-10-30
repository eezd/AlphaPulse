<script setup lang="ts">
import type { Analysis } from "@/common/apis/risk/type"
import { listRiskAnalysesApi } from "@/common/apis/risk"
import { useUserStore } from "@/pinia/stores/user"

const props = defineProps<{ newsId: number, revision: number }>()
const user = useUserStore()
const analysis = ref<Analysis | null>(null)
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
    failed.value = false
  }
  try {
    const rows = await listRiskAnalysesApi({ news_id: props.newsId, revision_no: props.revision }, reset)
    if (seq === sequence && version === user.sessionVersion) {
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
    if (!document.hidden && !loading.value && analysis.value?.state !== "done") void load(false)
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
    <b>AI 新闻分析</b>
    <p v-if="analysis?.state === 'done' && analysis.result">
      {{ analysis.result.verification === 'insufficient' ? '信息不足，无法判断' : analysis.result.stars ? '★'.repeat(analysis.result.stars) : '无明显风险' }} · {{ analysis.result.summary }}
    </p>
    <p v-else>
      {{ failed ? '分析状态读取失败' : analysis?.state === 'failed' ? `分析失败：${analysis.error}` : '等待分析，不能据此判断无风险' }}
      <el-button v-if="failed" link :loading="loading" @click="load()">
        重试
      </el-button>
    </p>
    <p v-if="analysis?.result" class="mb-0 text-xs">
      不确定性：{{ analysis.result.uncertainty }}
    </p>
  </div>
</template>
