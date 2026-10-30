<script setup lang="ts">
import { formatTime } from "@@/utils/text"
import { Refresh, Search } from "@element-plus/icons-vue"
import NewsDetail from "./components/NewsDetail.vue"
import { useNewsFeed } from "./composables/useNewsFeed"

const { sourceNames, filters, items, total, page, pageSize, loading, failed, pendingCount, openedId, listBox, connected, load, search } = useNewsFeed()
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
        <el-option v-for="(name, code) in sourceNames" :key="code" :value="code" :label="name" />
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
            <div class="line-clamp-2 text-sm text-slate-600 leading-relaxed">
              {{ item.summary || item.excerpt }}
            </div>
            <div class="mt-1 line-clamp-2 text-xs text-slate-500">
              <template v-if="item.risk?.state === 'done'">
                <span class="text-amber-600">{{ item.risk.verification === 'insufficient' ? '信息不足，无法判断' : item.risk.stars ? '★'.repeat(item.risk.stars) : '无明显风险' }}</span>
                · {{ item.risk.summary }}
              </template>
              <template v-else>
                {{ item.risk?.state === 'failed' ? 'AI 分析失败' : item.risk?.state === 'processing' ? 'AI 分析中' : 'AI 尚未分析' }}
              </template>
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
            layout="sizes, prev, pager, next, jumper" size="small" background
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
  flex: 0 0 40%;
}
@media (max-width: 760px) {
  .news-list {
    flex: 1;
  }
}
</style>
