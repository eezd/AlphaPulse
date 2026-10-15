<script lang="ts" setup>
import { CircleCheck, Cpu, Loading } from "@element-plus/icons-vue"
import { renderMarkdown } from "@/utils/markdown"
import { formatTime } from "@/utils/text"

export interface ToolCard {
  id: string
  name: string
  arguments: Record<string, unknown>
  /** 未返回时为 null */
  result: string | null
}

export interface DisplayItem {
  key: string
  kind: "user" | "assistant"
  text: string
  toolCalls: ToolCard[]
  stopped?: boolean
  error?: string
  /** 生成中：这次模型调用已结束，后续文字另起一条 */
  closed?: boolean
  /** 生成中：模型正在思考，还没有输出文字 */
  thinking?: boolean
  /** 已落库的消息时间；生成中的没有 */
  time?: string
}

defineProps<{ items: DisplayItem[] }>()

const TOOL_LABELS: Record<string, string> = {
  search_news: "检索新闻",
  get_news: "读取新闻正文",
  search_symbols: "查找交易对",
  get_klines: "获取 K 线",
  get_ticker: "获取 24h 行情",
  get_derivatives: "获取衍生品数据",
  list_skills: "列出 skills",
  read_skill: "读取 skill",
  propose_skill_edit: "提交 skill 草稿",
  create_skill: "新建 skill（草稿）",
  update_context: "更新 CONTEXT.md"
}

function prettyResult(result: string) {
  try {
    return JSON.stringify(JSON.parse(result), null, 2)
  } catch {
    // 前端拿到的是截断后的预览，可能不是完整 JSON
    return result
  }
}
</script>

<template>
  <div class="flex flex-col gap-5">
    <div v-for="item in items" :key="item.key" :class="item.kind === 'user' ? 'self-end max-w-3/4' : 'self-stretch'">
      <div v-if="item.kind === 'user'" class="border border-slate-200 rounded-lg border-solid bg-white px-4 py-3 shadow-sm">
        <div class="mb-1 flex justify-between gap-6 text-xs text-slate-500">
          <span class="text-[var(--el-color-primary)] font-semibold">你</span>
          <span class="num">{{ item.time ? formatTime(item.time) : "" }}</span>
        </div>
        <div class="whitespace-pre-wrap text-sm text-slate-800 leading-relaxed">
          {{ item.text }}
        </div>
      </div>
      <div v-else class="flex gap-3">
        <span class="h-8 w-8 flex-center shrink-0 rounded-full bg-[var(--el-color-primary)] text-white">
          <el-icon><Cpu /></el-icon>
        </span>
        <div class="min-w-0 flex flex-1 flex-col gap-2">
          <div class="flex items-center gap-2 text-xs">
            <span class="text-sm text-[var(--el-color-primary)] font-semibold">AlphaPulse AI</span>
            <span class="num text-slate-400">{{ item.time ? formatTime(item.time) : "" }}</span>
          </div>
          <div v-if="item.thinking" class="flex items-center gap-2 text-xs text-slate-500">
            <span class="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
            思考中…
          </div>
          <!-- markdown-it 已关闭 html，模型输出中的标签按文本显示 -->
          <div v-if="item.text" class="markdown-body" v-html="renderMarkdown(item.text)" />
          <el-collapse v-if="item.toolCalls.length" class="tool-cards">
            <el-collapse-item v-for="call in item.toolCalls" :key="call.id" :name="call.id">
              <template #title>
                <div class="flex items-center gap-2 overflow-hidden text-xs">
                  <el-icon v-if="call.result === null" class="is-loading text-amber-500">
                    <Loading />
                  </el-icon>
                  <el-icon v-else class="text-emerald-600">
                    <CircleCheck />
                  </el-icon>
                  <span class="shrink-0 text-[var(--el-color-primary)] font-semibold">{{ TOOL_LABELS[call.name] ?? call.name }}</span>
                  <span class="truncate text-slate-500 font-mono">{{ JSON.stringify(call.arguments) }}</span>
                </div>
              </template>
              <pre class="m-0 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 text-xs">{{ call.result === null ? "等待结果…" : prettyResult(call.result) }}</pre>
            </el-collapse-item>
          </el-collapse>
          <div v-if="item.stopped" class="text-xs text-slate-500">
            （已停止生成）
          </div>
          <el-alert v-if="item.error" :title="item.error" type="error" :closable="false" show-icon />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-cards {
  border: none;
}
.tool-cards :deep(.el-collapse-item) {
  margin-bottom: 6px;
  overflow: hidden;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
}
.tool-cards :deep(.el-collapse-item__header) {
  height: 34px;
  padding: 0 12px;
  border-bottom: none;
  line-height: 34px;
}
.tool-cards :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}
.tool-cards :deep(.el-collapse-item__content) {
  padding: 0 12px 10px;
}
</style>
