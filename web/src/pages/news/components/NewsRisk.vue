<script setup lang="ts">
import type { Package } from "@@/apis/news-research/type"
import { listNewsPackagesApi } from "@@/apis/news-research"
import { useUserStore } from "@/pinia/stores/user"

const props = defineProps<{ newsId: number, revision: number }>()
const user = useUserStore()
const summaries = ref<Package[]>([])
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
    summaries.value = []
    failed.value = false
  }
  try {
    const events = await listNewsPackagesApi(props.newsId, props.revision)
    if (seq === sequence && version === user.sessionVersion) {
      summaries.value = events
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
    if (!document.hidden && !loading.value && (!summaries.value.length || summaries.value.some(e => e.state !== "done"))) void load(false)
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
      <router-link :to="{ path: '/home', query: { package: event.id } }">
        {{ event.title }}
      </router-link>
      <div class="my-2 flex flex-wrap gap-2">
        <el-tag v-for="word in event.keywords" :key="word.canonical">
          {{ word.canonical }}
        </el-tag>
      </div>
      <p v-if="event.error">
        更新失败：{{ event.error }}
      </p>
    </div>
    <p v-if="!summaries.length">
      {{ failed ? '关联事件读取失败' : '尚未建立关键词索引' }}
      <el-button v-if="failed" link :loading="loading" @click="load()">
        重试
      </el-button>
    </p>
  </div>
</template>
