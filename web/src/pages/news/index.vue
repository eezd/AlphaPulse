<script lang="ts" setup>
import type { NewsBrief, NewsEvent } from "@/apis/news"
import { listNews } from "@/apis/news"
import { useEventSource } from "@/composables/useEventSource"
import { useSourceNames } from "@/composables/useSourceNames"
import NewsDrawer from "@/pages/news/components/NewsDrawer.vue"
import { formatTime } from "@/utils/text"

const sourceNames = useSourceNames()
const filters = reactive({
  source: [] as string[],
  q: "",
  range: null as [Date, Date] | null
})
const items = ref<NewsBrief[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const finished = ref(false)
const pendingCount = ref(0)
const openedId = ref<number | null>(null)

async function load(reset = false) {
  if (loading.value || (!reset && finished.value))
    return
  loading.value = true
  try {
    const page = await listNews({
      source: filters.source.length ? filters.source : undefined,
      q: filters.q || undefined,
      start: filters.range?.[0].toISOString(),
      end: filters.range?.[1].toISOString(),
      cursor: reset ? undefined : nextCursor.value ?? undefined,
      limit: 30
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    nextCursor.value = page.next_cursor
    finished.value = page.next_cursor === null
    if (reset)
      pendingCount.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  finished.value = false
  load(true)
}

// 只提示符合当前来源筛选的新条目；关键字和时间范围由刷新后的查询决定
const { connected } = useEventSource<NewsEvent>("/api/news/stream", "news", (event) => {
  if (event.kind === "new" && (filters.source.length === 0 || filters.source.includes(event.source_code)))
    pendingCount.value++
})

load(true)
</script>

<template>
  <div class="h-full flex flex-col gap-3">
    <div class="flex flex-wrap items-center gap-3">
      <el-checkbox-group v-model="filters.source" @change="search">
        <el-checkbox-button v-for="(name, code) in sourceNames" :key="code" :value="code">
          {{ name }}
        </el-checkbox-button>
      </el-checkbox-group>
      <el-input v-model="filters.q" placeholder="关键字" clearable class="w-60" @keyup.enter="search" @clear="search" />
      <el-date-picker
        v-model="filters.range" type="datetimerange" start-placeholder="开始" end-placeholder="结束"
        class="max-w-90" @change="search"
      />
      <el-button type="primary" @click="search">
        查询
      </el-button>
      <el-tag :type="connected ? 'success' : 'danger'" size="small">
        {{ connected ? "实时推送已连接" : "实时推送断开" }}
      </el-tag>
    </div>

    <el-alert
      v-if="pendingCount > 0" :title="`有 ${pendingCount} 条新消息，点击刷新`" type="info" :closable="false"
      class="cursor-pointer" @click="search"
    />

    <div v-infinite-scroll="() => load()" :infinite-scroll-disabled="loading || finished" class="min-h-0 flex-1 overflow-auto">
      <div
        v-for="item in items" :key="item.id"
        class="cursor-pointer border-b border-b-solid border-[var(--el-border-color-lighter)] px-2 py-3 hover:bg-[var(--el-fill-color-light)]"
        @click="openedId = item.id"
      >
        <div class="mb-1 flex items-center gap-2 text-xs text-[var(--el-text-color-secondary)]">
          <el-tag size="small">
            {{ sourceNames[item.source_code] ?? item.source_code }}
          </el-tag>
          <span>{{ formatTime(item.published_at) }}</span>
          <el-tag v-if="item.revision_no > 1" size="small" type="warning">
            已修订 {{ item.revision_no - 1 }} 次
          </el-tag>
          <el-tag v-if="item.is_paywalled" size="small" type="info">
            付费
          </el-tag>
        </div>
        <div v-if="item.title" class="font-bold">
          {{ item.title }}
        </div>
        <div class="line-clamp-3 text-sm text-[var(--el-text-color-regular)]">
          {{ item.summary || item.excerpt }}
        </div>
      </div>
      <div class="py-4 text-center text-sm text-[var(--el-text-color-secondary)]">
        {{ loading ? "加载中…" : finished ? "没有更多了" : "" }}
      </div>
    </div>

    <NewsDrawer v-model="openedId" />
  </div>
</template>
