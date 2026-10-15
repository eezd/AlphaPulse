<script lang="ts" setup>
import type { NewsBrief, NewsEvent } from "@/apis/news"
import { Refresh, Search } from "@element-plus/icons-vue"
import { listNews } from "@/apis/news"
import { useEventSource } from "@/composables/useEventSource"
import { useSourceNames } from "@/composables/useSourceNames"
import NewsDetail from "@/pages/news/components/NewsDetail.vue"
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
    <div class="flex items-center gap-3">
      <el-tag :type="connected ? 'success' : 'danger'">
        ● {{ connected ? "实时推送已连接" : "实时推送断开" }}
      </el-tag>
      <div class="flex-1" />
      <div
        v-if="pendingCount > 0"
        class="flex cursor-pointer items-center gap-1 border border-sky-200 rounded-full border-solid bg-sky-50 px-4 py-1 text-sm text-[var(--el-color-primary)] hover:bg-sky-100"
        @click="search"
      >
        <el-icon><Refresh /></el-icon>
        有 {{ pendingCount }} 条新消息，点击刷新
      </div>
    </div>

    <el-card shadow="never" body-class="flex flex-wrap items-center gap-3">
      <span class="text-xs text-slate-500">来源</span>
      <el-checkbox-group v-model="filters.source" @change="search">
        <el-checkbox v-for="(name, code) in sourceNames" :key="code" :value="code" border class="mr-2!">
          {{ name }}
        </el-checkbox>
      </el-checkbox-group>
      <el-input v-model="filters.q" :prefix-icon="Search" placeholder="关键字" clearable class="w-60" @keyup.enter="search" @clear="search" />
      <el-date-picker
        v-model="filters.range" type="datetimerange" start-placeholder="开始" end-placeholder="结束"
        class="max-w-90" @change="search"
      />
      <el-button type="primary" :icon="Search" @click="search">
        查询
      </el-button>
    </el-card>

    <div class="min-h-0 flex flex-1 gap-4">
      <el-card shadow="never" class="w-2/5 flex shrink-0 flex-col" body-class="min-h-0 flex-1 flex flex-col p-0">
        <template #header>
          <div class="flex items-center justify-between">
            消息流
            <span class="num text-xs text-slate-500 font-normal">共 {{ total.toLocaleString() }} 条</span>
          </div>
        </template>
        <div ref="listBox" v-loading="loading" class="min-h-0 flex flex-1 flex-col gap-2 overflow-auto p-3">
          <div
            v-for="item in items" :key="item.id"
            class="cursor-pointer border rounded-lg border-solid p-3 transition-colors"
            :class="item.id === openedId ? 'border-[var(--el-color-primary)] bg-sky-50/50' : 'border-slate-200 hover:border-slate-300 hover:bg-slate-50'"
            @click="openedId = item.id"
          >
            <div class="mb-1 flex items-center gap-2 text-xs text-slate-500">
              <el-tag size="small">
                {{ sourceNames[item.source_code] ?? item.source_code }}
              </el-tag>
              <span class="num">{{ formatTime(item.published_at) }}</span>
              <div class="flex-1" />
              <el-tag v-if="item.revision_no > 1" size="small" type="warning">
                已修订 {{ item.revision_no - 1 }} 次
              </el-tag>
              <el-tag v-if="item.is_paywalled" size="small" type="info">
                付费
              </el-tag>
            </div>
            <div v-if="item.title" class="mb-1 text-slate-900 font-semibold">
              {{ item.title }}
            </div>
            <div class="line-clamp-3 text-sm text-slate-600 leading-relaxed">
              {{ item.summary || item.excerpt }}
            </div>
          </div>
          <el-empty v-if="!loading && items.length === 0" description="没有符合条件的新闻" />
        </div>
        <div class="border-t border-t-slate-200 border-t-solid px-3 py-2">
          <el-pagination
            v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 30, 50, 100]" :pager-count="5"
            layout="sizes, prev, pager, next, jumper" size="small" background
            @current-change="load" @size-change="changePageSize"
          />
        </div>
      </el-card>

      <NewsDetail :news-id="openedId" class="min-w-0 flex-1" />
    </div>
  </div>
</template>
