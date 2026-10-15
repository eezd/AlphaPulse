<script lang="ts" setup>
import type { NewsDetail, Revision } from "@/apis/news"
import { getNews, listRevisions } from "@/apis/news"
import { useSourceNames } from "@/composables/useSourceNames"
import { diffLines, formatTime, htmlToLines } from "@/utils/text"

const newsId = defineModel<number | null>({ required: true })

const sourceNames = useSourceNames()
const detail = ref<NewsDetail>()
const revisions = ref<Revision[]>([])
const activeTab = ref("content")
// 当前查看的修订序号，与上一版对比
const selectedRevision = ref<number>()

watch(newsId, async (id) => {
  if (id == null)
    return
  activeTab.value = "content"
  detail.value = undefined
  const [news, revs] = await Promise.all([getNews(id), listRevisions(id)])
  detail.value = news
  revisions.value = revs
  selectedRevision.value = revs[0]?.revision_no
})

// 第三方正文放进无脚本权限的 sandbox iframe，避免其中的脚本访问本站
const srcdoc = computed(() => detail.value && `<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><base target="_blank">
<style>body{font:15px/1.8 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#303133;margin:0 4px}img{max-width:100%;height:auto}</style>
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
</script>

<template>
  <el-drawer :model-value="newsId != null" size="50%" :with-header="false" @close="newsId = null">
    <div v-if="detail" class="h-full flex flex-col gap-3">
      <div class="text-lg font-bold">
        {{ detail.title || detail.excerpt.slice(0, 60) }}
      </div>
      <div class="flex flex-wrap items-center gap-2 text-sm text-[var(--el-text-color-secondary)]">
        <el-tag size="small">
          {{ sourceNames[detail.source_code] ?? detail.source_code }}
        </el-tag>
        <span>{{ formatTime(detail.published_at, "YYYY-MM-DD HH:mm:ss") }}</span>
        <span v-if="detail.author">{{ detail.author }}</span>
        <el-tag v-if="detail.is_paywalled" size="small" type="warning">
          付费（仅公开部分）
        </el-tag>
        <el-tag v-for="tag in detail.tags" :key="tag" size="small" type="info">
          {{ tag }}
        </el-tag>
        <el-link :href="detail.url" target="_blank" type="primary">
          原文
        </el-link>
      </div>
      <el-tabs v-model="activeTab" class="min-h-0 flex flex-1 flex-col">
        <el-tab-pane label="正文" name="content" class="h-full">
          <iframe :srcdoc="srcdoc" sandbox="allow-popups allow-popups-to-escape-sandbox" class="h-full w-full border-0" />
        </el-tab-pane>
        <el-tab-pane :label="`修订（${revisions.length}）`" name="revisions" class="h-full overflow-auto">
          <el-radio-group v-model="selectedRevision" size="small" class="mb-3">
            <el-radio-button v-for="r in revisions" :key="r.revision_no" :value="r.revision_no">
              v{{ r.revision_no }} · {{ formatTime(r.captured_at) }}
            </el-radio-button>
          </el-radio-group>
          <div class="text-sm leading-7">
            <div
              v-for="(line, i) in diff" :key="i"
              :class="{ 'bg-green-100': line.type === 'add', 'bg-red-100 line-through': line.type === 'del' }"
              class="px-2"
            >
              {{ line.type === "add" ? "+ " : line.type === "del" ? "- " : "  " }}{{ line.text }}
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
    <div v-else v-loading="true" class="h-full" />
  </el-drawer>
</template>
