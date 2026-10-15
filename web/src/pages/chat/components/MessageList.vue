<script lang="ts" setup>
import { renderMarkdown } from "@/utils/markdown"

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
  <div class="flex flex-col gap-4">
    <div v-for="item in items" :key="item.key" :class="item.kind === 'user' ? 'self-end max-w-3/4' : 'self-stretch'">
      <div v-if="item.kind === 'user'" class="whitespace-pre-wrap border-l-2 border-l-ap-brand border-l-solid bg-ap-panel-high px-3 py-2 font-sans text-sm">
        {{ item.text }}
      </div>
      <div v-else class="flex flex-col gap-2">
        <!-- markdown-it 已关闭 html，模型输出中的标签按文本显示 -->
        <div v-if="item.thinking" class="text-xs text-[var(--el-text-color-secondary)]">
          思考中…
        </div>
        <div v-if="item.text" class="markdown-body" v-html="renderMarkdown(item.text)" />
        <el-collapse v-if="item.toolCalls.length" class="tool-cards">
          <el-collapse-item v-for="call in item.toolCalls" :key="call.id" :name="call.id">
            <template #title>
              <div class="flex items-center gap-2 overflow-hidden text-xs">
                <el-tag size="small" :type="call.result === null ? 'warning' : 'info'">
                  {{ call.result === null ? "执行中" : "工具" }}
                </el-tag>
                <span class="font-bold">{{ TOOL_LABELS[call.name] ?? call.name }}</span>
                <span class="truncate text-[var(--el-text-color-secondary)] font-mono">{{ JSON.stringify(call.arguments) }}</span>
              </div>
            </template>
            <pre class="m-0 max-h-80 overflow-auto whitespace-pre-wrap break-all border border-ap-line border-solid bg-ap-panel-low p-2 text-xs">{{ call.result === null ? "等待结果…" : prettyResult(call.result) }}</pre>
          </el-collapse-item>
        </el-collapse>
        <div v-if="item.stopped" class="text-xs text-[var(--el-text-color-secondary)]">
          （已停止生成）
        </div>
        <el-alert v-if="item.error" :title="item.error" type="error" :closable="false" show-icon />
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-cards {
  border: 1px solid var(--ap-line);
}
.tool-cards :deep(.el-collapse-item__header) {
  height: 32px;
  line-height: 32px;
}
</style>
