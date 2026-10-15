<script lang="ts" setup>
import type { NewsBrief, NewsEvent } from "@/apis/news"
import { listNews } from "@/apis/news"
import { useEventSource } from "@/composables/useEventSource"
import { useSourceNames } from "@/composables/useSourceNames"
import NewsDrawer from "@/pages/news/components/NewsDrawer.vue"
import { usePersisted } from "@/utils/storage"
import { formatTime } from "@/utils/text"

const sourceNames = useSourceNames()
const filters = reactive({
  source: [] as string[],
  q: "",
  range: null as [Date, Date] | null
})
const items = ref<NewsBrief[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = usePersisted("news-page-size", 30)
const loading = ref(false)
const pendingCount = ref(0)
const openedId = ref<number | null>(null)
const listBox = ref<HTMLElement>()

// 多次快速翻页时只采用最后一次请求的结果
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    const result = await listNews({
      source: filters.source.length ? filters.source : undefined,
      q: filters.q || undefined,
      start: filters.range?.[0].toISOString(),
      end: filters.range?.[1].toISOString(),
      page: page.value,
      limit: pageSize.value
    })
    if (seq !== requestSeq)
      return
    items.value = result.items
    total.value = result.total
    if (page.value === 1)
      pendingCount.value = 0
    listBox.value?.scrollTo({ top: 0 })
  } finally {
    if (seq === requestSeq)
      loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function changePageSize() {
  page.value = 1
  load()
}

// 只提示符合当前来源筛选的新条目；关键字和时间范围由刷新后的查询决定
const { connected } = useEventSource<NewsEvent>("/api/news/stream", "news", (event) => {
  if (event.kind === "new" && (filters.source.length === 0 || filters.source.includes(event.source_code)))
    pendingCount.value++
})

load()
</script>

<template>
  <div class="h-full flex flex-col gap-3">
    <div class="flex flex-wrap items-center gap-3 border border-ap-line border-solid bg-ap-panel px-3 py-2">
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
      <div class="flex-1" />
      <el-tag :type="connected ? 'success' : 'danger'" size="small">
        <i class="mr-1.5 inline-block h-1.5 w-1.5 bg-current" />{{ connected ? "实时推送已连接" : "实时推送断开" }}
      </el-tag>
    </div>

    <el-alert
      v-if="pendingCount > 0" :title="`有 ${pendingCount} 条新消息，点击刷新`" type="info" :closable="false"
      class="cursor-pointer" @click="search"
    />

    <div ref="listBox" v-loading="loading" class="min-h-0 flex-1 overflow-auto border border-ap-line border-solid bg-ap-panel">
      <div
        v-for="item in items" :key="item.id"
        class="cursor-pointer border-b border-b-ap-line/50 border-b-solid px-3 py-2.5 hover:bg-ap-panel-high"
        @click="openedId = item.id"
      >
        <div class="mb-1 flex items-center gap-2 text-xs text-[var(--el-text-color-secondary)]">
          <el-tag size="small" type="info">
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
        <div v-if="item.title" class="mb-0.5 font-sans text-[14px] font-bold">
          {{ item.title }}
        </div>
        <div class="line-clamp-3 font-sans text-[13px] text-ap-muted leading-relaxed">
          {{ item.summary || item.excerpt }}
        </div>
      </div>
      <el-empty v-if="!loading && items.length === 0" description="没有符合条件的新闻" />
    </div>

    <el-pagination
      v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 30, 50, 100]"
      layout="total, sizes, prev, pager, next, jumper" background class="justify-end"
      @current-change="load" @size-change="changePageSize"
    />

    <NewsDrawer v-model="openedId" />
  </div>
</template>
