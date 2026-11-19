<script setup lang="ts">
import type { SourceInfo } from "@@/apis/crawler/type"
import { listSourcesApi } from "@@/apis/crawler"
import { useSessionScope } from "@@/composables/useSessionScope"
import { formatTime } from "@@/utils/text"
import { Refresh, Search } from "@element-plus/icons-vue"
import NewsDetail from "./components/NewsDetail.vue"
import { useNewsFeed } from "./composables/useNewsFeed"

const { sourceNames, filters, items, total, page, pageSize, loading, failed, riskFailed, pendingCount, openedId, listBox, connected, load, search, savingBookmarks, toggleBookmark } = useNewsFeed()
const route = useRoute()
const compact = ref(false)
const sourceCounts = ref<Record<string, Pick<SourceInfo, "total_items" | "today_new">>>({})
const sessionScope = useSessionScope()
let countTimer: ReturnType<typeof setInterval> | undefined
let countRequest = 0
async function refreshSourceCounts() {
  const request = ++countRequest
  try {
    const info = await listSourcesApi(false)
    if (request !== countRequest || !sessionScope.isCurrent()) return
    sourceCounts.value = Object.fromEntries(info.sources.map(source => [source.code, {
      total_items: source.total_items,
      today_new: source.today_new
    }]))
  } catch {
    if (request === countRequest && sessionScope.isCurrent()) sourceCounts.value = {}
  }
}
onMounted(() => {
  void refreshSourceCounts()
  countTimer = setInterval(() => {
    if (!document.hidden) void refreshSourceCounts()
  }, 60_000)
})
onBeforeUnmount(() => {
  clearInterval(countTimer)
  countRequest++
})
watch(() => route.query.news, (value) => {
  if (typeof value === "string" && /^\d+$/.test(value)) openedId.value = Number(value)
}, { immediate: true })
function resetFilters() {
  Object.assign(filters, { source: [], research_only: false, author: "", content_status: "", q: "", range: null, processing_state: "", bookmarked: false, sort: "desc" })
  void search()
}
function recent(hours: number) {
  filters.range = [new Date(Date.now() - hours * 3600000), new Date()]
  void search()
}
const mobile = ref(false)
let media: MediaQueryList
const updateMobile = () => mobile.value = media.matches
onMounted(() => {
  media = window.matchMedia("(max-width: 760px)")
  updateMobile()
  media.addEventListener("change", updateMobile)
})
onBeforeUnmount(() => media?.removeEventListener("change", updateMobile))
</script>

<template>
  <div class="h-full flex flex-col gap-3">
    <div class="flex items-center gap-3">
      <el-tag :type="connected ? 'success' : 'danger'">
        ● {{ connected ? "实时推送已连接" : "实时推送断开" }}
      </el-tag>
      <div class="flex-1" />
      <span v-if="riskFailed" class="text-xs text-amber-600">AI 状态刷新失败，将自动重试</span>
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
      <el-select v-model="filters.source" multiple collapse-tags collapse-tags-tooltip filterable clearable class="w-64" placeholder="全部来源" @change="search">
        <el-option v-for="(name, code) in sourceNames" :key="code" :value="code" :label="name" class="h-auto! py-1.5!">
          <div class="min-w-0 leading-5">
            <div class="truncate">
              {{ name }}
            </div>
            <div class="text-xs text-slate-500">
              累计 {{ sourceCounts[code]?.total_items.toLocaleString() ?? "—" }} · 今日 {{ sourceCounts[code] ? `+${sourceCounts[code].today_new.toLocaleString()}` : "—" }}
            </div>
          </div>
        </el-option>
      </el-select>
      <el-input v-model="filters.q" :prefix-icon="Search" placeholder="关键字" clearable class="w-60" @keyup.enter="search" @clear="search" />
      <el-date-picker
        v-model="filters.range" type="datetimerange" start-placeholder="开始" end-placeholder="结束"
        class="max-w-90" @change="search"
      />
      <el-button type="primary" :icon="Search" @click="search">
        查询
      </el-button>
    </el-card>

    <div class="flex flex-wrap items-center gap-2">
      <el-checkbox v-model="filters.research_only" @change="search">
        研究来源文章
      </el-checkbox>
      <el-input v-model="filters.author" placeholder="原始署名" clearable class="w-48" @keyup.enter="search" @clear="search" />
      <el-select v-model="filters.content_status" clearable placeholder="正文完整性" class="w-40" @change="search">
        <el-option v-for="(label, value) in { full: '网页全文', excerpt: '公开摘要', metadata: '仅元数据', extraction_failed: '正文提取失败' }" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select v-model="filters.processing_state" clearable placeholder="全部处理状态" class="w-40" @change="search">
        <el-option v-for="(label, value) in { unprocessed: '尚未分析', pending: '等待分析', processing: '分析中', done: '已分析', failed: '分析失败', interrupted: '已中断' }" :key="value" :label="label" :value="value" />
      </el-select>
      <el-checkbox v-model="filters.bookmarked" @change="search">
        只看收藏
      </el-checkbox>
      <el-select v-model="filters.sort" class="w-32" @change="search">
        <el-option label="最新优先" value="desc" /><el-option label="最早优先" value="asc" />
      </el-select>
      <el-button @click="recent(24)">
        过去 24 小时
      </el-button><el-button @click="recent(168)">
        过去 7 天
      </el-button><el-button @click="resetFilters">
        重置
      </el-button><el-checkbox v-model="compact">
        紧凑模式
      </el-checkbox>
    </div>
    <div class="news-workspace min-h-0 flex flex-1 gap-3">
      <el-card shadow="never" class="news-list min-w-0 flex flex-col" body-class="min-h-0 flex-1 flex flex-col p-0">
        <template #header>
          <div class="flex items-center justify-between">
            消息流
            <span class="num text-xs text-slate-500 font-normal">共 {{ total.toLocaleString() }} 条</span>
          </div>
        </template>
        <div ref="listBox" v-loading="loading" class="min-h-0 flex flex-1 flex-col overflow-auto">
          <div
            v-for="item in items" :key="item.id"
            class="cursor-pointer border-b border-solid px-3 py-2 transition-colors"
            :class="item.id === openedId ? 'border-[var(--el-color-primary)] bg-sky-50/50' : 'border-slate-200 hover:border-slate-300 hover:bg-slate-50'"
            @click="openedId = item.id"
          >
            <div class="mb-1 flex items-center gap-2 text-xs text-slate-500">
              <el-tag size="small">
                {{ sourceNames[item.source_code] ?? item.source_code }}
              </el-tag>
              <span class="num">{{ item.published_at_precision === "unknown" ? "发布时间未知" : item.published_at_precision === "date" ? item.published_date : formatTime(item.published_at) }}</span>
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
            <div v-if="!compact" class="line-clamp-2 text-sm text-slate-600 leading-relaxed">
              {{ item.summary || item.excerpt }}
            </div>
            <div class="flex flex-wrap gap-1 my-2">
              <el-tag v-for="word in item.keywords" :key="word.canonical" size="small">
                {{ word.canonical }}
              </el-tag><el-button link :loading="savingBookmarks.has(item.id)" @click.stop="toggleBookmark(item)">
                {{ item.bookmarked ? '★ 已收藏' : '☆ 收藏' }}
              </el-button>
            </div>
            <div class="mt-1 line-clamp-2 text-xs text-slate-500">
              <span v-if="item.analysis_admission" :title="item.analysis_admission.reason">{{ { collect_only: '仅采集', archive: '研究资料库', report: '参与报告' }[item.analysis_admission.mode] }} · </span>
              {{ item.analysis_admission?.mode === 'collect_only' ? item.analysis_admission.reason : item.analysis?.result?.summary || (item.analysis ? `AI ${item.analysis.state}` : '尚未进行 AI 分析') }}
            </div>
          </div>
          <el-alert v-if="failed" type="error" :closable="false" title="新闻读取失败；已有内容保留">
            <el-button link @click="load">
              重试
            </el-button>
          </el-alert>
          <el-empty v-if="!loading && !failed && items.length === 0" description="没有符合条件的新闻" />
        </div>
        <div class="border-t border-t-slate-200 border-t-solid px-3 py-2">
          <el-pagination
            v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 30, 50, 100]" :pager-count="5"
            :layout="mobile ? 'prev, pager, next, total' : 'sizes, prev, pager, next, jumper'" size="small" background
          />
        </div>
      </el-card>

      <NewsDetail v-if="!mobile" :news-id="openedId" class="min-w-0 flex-1" />
    </div>
    <el-drawer :model-value="mobile && openedId !== null" title="新闻详情" size="100%" @close="openedId = null">
      <NewsDetail v-if="mobile" :news-id="openedId" class="h-full" />
    </el-drawer>
  </div>
</template>

<style scoped>
.news-list {
  flex: 0 0 62%;
}
@media (max-width: 760px) {
  .news-list {
    flex: 1;
  }
}
</style>
