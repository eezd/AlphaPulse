<script lang="ts" setup>
import type { NewsDetail, Revision } from "@/apis/news"
import { Link } from "@element-plus/icons-vue"
import { getNews, listRevisions } from "@/apis/news"
import { useSourceNames } from "@/composables/useSourceNames"
import { diffLines, formatTime, htmlToLines } from "@/utils/text"

const props = defineProps<{ newsId: number | null }>()

const sourceNames = useSourceNames()
const detail = ref<NewsDetail>()
const revisions = ref<Revision[]>([])
const activeTab = ref("content")
// 当前查看的修订序号，与上一版对比
const selectedRevision = ref<number>()
// 加载失败（错误提示已由 http 拦截器弹出）：停止转圈，显示重试
const failed = ref(false)
// 连续点击不同新闻时，只采用最后一次请求的结果
let requestSeq = 0

async function load(id: number) {
  const seq = ++requestSeq
  failed.value = false
  try {
    const [news, revs] = await Promise.all([getNews(id), listRevisions(id)])
    if (seq !== requestSeq)
      return
    detail.value = news
    revisions.value = revs
    selectedRevision.value = revs[0]?.revision_no
  } catch (error) {
    if (seq === requestSeq)
      failed.value = true
    throw error
  }
}

watch(() => props.newsId, (id) => {
  activeTab.value = "content"
  detail.value = undefined
  revisions.value = []
  if (id == null)
    requestSeq++
  else
    load(id)
})

// 第三方正文放进无脚本权限的 sandbox iframe，避免其中的脚本访问本站
const srcdoc = computed(() => detail.value && `<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><base target="_blank">
<style>body{font:15px/1.8 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#334155;margin:0 4px}img{max-width:100%;height:auto}</style>
</head><body>${detail.value.content_html}</body></html>`)

const diff = computed(() => {
  const index = revisions.value.findIndex(r => r.revision_no === selectedRevision.value)
  if (index < 0)
    return []
  const current = revisions.value[index]
  const previous = revisions.value[index + 1]
  const toLines = (r?: Revision) => r ? [r.title, r.summary, ...htmlToLines(r.content_html)].filter(Boolean) : []
  return diffLines(toLines(previous), toLines(current))
})

const diffStats = computed(() => ({
  add: diff.value.filter(line => line.type === "add").length,
  del: diff.value.filter(line => line.type === "del").length
}))
</script>

<template>
  <el-card shadow="never" class="flex flex-col" body-class="min-h-0 flex-1 flex flex-col gap-3">
    <el-empty v-if="newsId == null" description="点击左侧新闻查看详情" class="m-auto" />
    <template v-else-if="detail">
      <div class="flex flex-wrap items-center gap-2">
        <el-tag size="small">
          {{ sourceNames[detail.source_code] ?? detail.source_code }}
        </el-tag>
        <el-tag v-if="detail.is_paywalled" size="small" type="warning">
          付费（仅公开部分）
        </el-tag>
      </div>
      <div class="text-xl text-slate-900 font-bold leading-snug">
        {{ detail.title || detail.excerpt.slice(0, 60) }}
      </div>
      <div class="grid grid-cols-2 gap-x-6 gap-y-2 border border-slate-200 rounded-lg border-solid bg-slate-50 px-3 py-2 text-sm">
        <div>
          <span class="label">发布时间</span><span class="num">{{ formatTime(detail.published_at, "YYYY-MM-DD HH:mm:ss") }}</span>
        </div>
        <div>
          <span class="label">作者</span>{{ detail.author || "-" }}
        </div>
        <div class="flex flex-wrap items-center gap-1">
          <span class="label">标签</span>
          <el-tag v-for="tag in detail.tags" :key="tag" size="small" type="info">
            {{ tag }}
          </el-tag>
          <span v-if="!detail.tags.length">-</span>
        </div>
        <div class="text-right">
          <el-link :href="detail.url" target="_blank" type="primary" :icon="Link">
            原文链接
          </el-link>
        </div>
      </div>
      <el-tabs v-model="activeTab" class="min-h-0 flex flex-1 flex-col">
        <el-tab-pane label="正文" name="content" class="h-full">
          <iframe :srcdoc="srcdoc" sandbox="allow-popups allow-popups-to-escape-sandbox" class="h-full w-full border-0" />
        </el-tab-pane>
        <el-tab-pane :label="`修订（${revisions.length}）`" name="revisions" class="h-full overflow-auto">
          <div class="mb-3 flex flex-wrap items-center gap-3 border border-slate-200 rounded-lg border-solid bg-slate-50 px-3 py-2">
            <span class="text-xs text-slate-500">比对版本</span>
            <el-select v-model="selectedRevision" size="small" class="w-64">
              <el-option
                v-for="(r, i) in revisions" :key="r.revision_no" :value="r.revision_no"
                :label="`v${r.revision_no} · ${formatTime(r.captured_at)}${revisions[i + 1] ? ` vs v${revisions[i + 1].revision_no}` : '（首版）'}`"
              />
            </el-select>
            <span class="num text-xs text-emerald-700">+{{ diffStats.add }} 行</span>
            <span class="num text-xs text-rose-700">−{{ diffStats.del }} 行</span>
          </div>
          <div class="overflow-hidden border border-slate-200 rounded-lg border-solid text-sm leading-7">
            <div
              v-for="(line, i) in diff" :key="i" class="flex gap-2 px-3"
              :class="{ 'bg-emerald-50 text-emerald-900': line.type === 'add', 'bg-rose-50 text-rose-900 line-through': line.type === 'del' }"
            >
              <span class="num w-3 shrink-0 text-slate-400">{{ line.type === "add" ? "+" : line.type === "del" ? "−" : "" }}</span>
              <span class="min-w-0">{{ line.text }}</span>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>
    <el-empty v-else-if="failed" description="加载失败" class="m-auto">
      <el-button type="primary" @click="load(newsId)">
        重试
      </el-button>
    </el-empty>
    <div v-else v-loading="true" class="h-full" />
  </el-card>
</template>

<style scoped>
.label {
  margin-right: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
