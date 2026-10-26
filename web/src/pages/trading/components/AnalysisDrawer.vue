<script lang="ts" setup>
import type { AnalysisDetail } from "@@/apis/trading/type"
import { getAnalysisApi } from "@@/apis/trading"
import { renderMarkdown } from "@@/utils/markdown"
import { formatTime } from "@@/utils/text"

const props = defineProps<{ analysisId: number | null }>()
const visible = defineModel<boolean>({ required: true })

const detail = ref<AnalysisDetail>()

watch(() => [visible.value, props.analysisId] as const, async ([open, id]) => {
  detail.value = undefined
  if (open && id)
    detail.value = await getAnalysisApi(id)
})

const toolResults = computed(() => {
  const results = new Map<string, string>()
  for (const m of detail.value?.transcript ?? []) {
    if (m.role === "tool")
      results.set(m.tool_call_id, m.result)
  }
  return results
})

const duration = computed(() => detail.value?.finished_at ? Math.round((new Date(detail.value.finished_at).getTime() - new Date(detail.value.started_at).getTime()) / 1000) : null)
const toolCallCount = computed(() => (detail.value?.transcript ?? []).reduce((n, m) => n + (m.role === "assistant" ? m.tool_calls.length : 0), 0))

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

function inlineJson(value: unknown): string {
  if (Array.isArray(value))
    return `[${value.map(inlineJson).join(", ")}]`
  if (value && typeof value === "object")
    return `{${Object.entries(value).map(([k, v]) => `${JSON.stringify(k)}: ${inlineJson(v)}`).join(", ")}}`
  return JSON.stringify(value)
}

/** 缩进展开，但放得下一行的数组、对象不再拆开：K 线每根一行，而不是每个数字一行 */
function formatJson(value: unknown, indent = ""): string {
  const flat = inlineJson(value)
  if (flat.length <= 100 || !value || typeof value !== "object")
    return flat
  const inner = `${indent}  `
  if (Array.isArray(value))
    return `[\n${value.map(v => inner + formatJson(v, inner)).join(",\n")}\n${indent}]`
  return `{\n${Object.entries(value).map(([k, v]) => `${inner}${JSON.stringify(k)}: ${formatJson(v, inner)}`).join(",\n")}\n${indent}}`
}

function formatResult(raw: string | undefined) {
  if (raw === undefined)
    return "无结果"
  const value = parseJson(raw)
  return value === undefined ? raw : formatJson(value)
}

/** 发给模型的消息是 Markdown，其中整行的 JSON（预取行情、最近决策）改成格式化的代码块 */
function promptMarkdown(text: string) {
  return text.split("\n").map((line) => {
    const start = line.indexOf("{")
    const value = start < 0 ? undefined : parseJson(line.slice(start))
    if (value === undefined)
      return line
    const prefix = line.slice(0, start)
    // 标题里的是工具参数，短，保持在标题行内
    if (prefix.startsWith("#"))
      return `${prefix}\`${inlineJson(value)}\``
    // 列表项里的代码块要缩进到列表内容的位置，才属于这一项
    const pad = " ".repeat(/^\s*[-*] /.exec(prefix)?.[0].length ?? 0)
    const block = ["```json", ...formatJson(value).split("\n"), "```"].map(l => pad + l).join("\n")
    return `${prefix}\n\n${block}\n`
  }).join("\n")
}
</script>

<template>
  <el-drawer v-model="visible" :title="detail ? `分析 #${detail.id} · ${detail.symbol}` : '分析'" size="55%">
    <div v-if="detail" class="flex flex-col gap-3 text-sm">
      <div class="grid grid-cols-4 gap-2">
        <div class="meta">
          <div class="meta-label">
            分析 ID
          </div>
          <div class="num text-slate-900">
            #{{ detail.id }}{{ detail.dry_run ? " · 试运行" : "" }}
          </div>
        </div>
        <div class="meta">
          <div class="meta-label">
            时间
          </div>
          <div class="num text-slate-900">
            {{ formatTime(detail.started_at, "MM-DD HH:mm:ss") }}
          </div>
          <div class="num text-xs text-slate-500">
            {{ duration === null ? "未结束" : `耗时 ${duration}s` }}
          </div>
        </div>
        <div class="meta">
          <div class="meta-label">
            token（入 / 出）
          </div>
          <div class="num text-slate-900">
            {{ detail.usage?.input_tokens.toLocaleString() ?? "-" }} / {{ detail.usage?.output_tokens.toLocaleString() ?? "-" }}
          </div>
        </div>
        <div class="meta">
          <div class="meta-label">
            模型 / 思考强度
          </div>
          <div class="num truncate text-slate-900" :title="detail.model ?? ''">
            {{ detail.model ?? "-" }}
          </div>
          <div class="num text-xs text-slate-500">
            {{ detail.reasoning_effort ?? "跟随配置" }}
          </div>
        </div>
      </div>
      <div class="text-xs text-slate-500">
        触发：{{ detail.reasons.join("；") }}
      </div>
      <el-alert v-if="detail.error" type="error" :closable="false" :title="detail.error" />
      <el-card v-if="detail.text" shadow="never">
        <template #header>
          模型分析
        </template>
        <!-- markdown-it 已关闭 html，模型输出中的标签按文本显示 -->
        <div class="markdown-body" v-html="renderMarkdown(detail.text)" />
      </el-card>
      <el-card shadow="never" body-class="py-0">
        <template #header>
          发给模型的数据与工具调用（{{ toolCallCount }}）
        </template>
        <el-collapse>
          <template v-for="(m, i) in detail.transcript ?? []" :key="i">
            <el-collapse-item v-if="m.role === 'user'" :name="`u${i}`" :title="i === 0 ? '发给模型的数据（账户、持仓、预取行情）' : '程序提示'">
              <div class="prompt markdown-body max-h-160 overflow-auto pr-1" v-html="renderMarkdown(promptMarkdown(m.text))" />
            </el-collapse-item>
            <template v-else-if="m.role === 'assistant'">
              <el-collapse-item v-for="call in m.tool_calls" :key="call.id" :name="call.id">
                <template #title>
                  <span class="flex items-center gap-2 overflow-hidden">
                    <span class="text-slate-900 font-semibold font-mono">{{ call.name }}</span>
                    <span class="truncate text-xs text-slate-400 font-mono">{{ inlineJson(call.arguments) }}</span>
                  </span>
                </template>
                <div class="block-label">
                  参数
                </div>
                <pre class="code-block">{{ formatJson(call.arguments) }}</pre>
                <div class="block-label mt-3">
                  返回
                </div>
                <pre class="code-block">{{ formatResult(toolResults.get(call.id)) }}</pre>
              </el-collapse-item>
            </template>
          </template>
        </el-collapse>
      </el-card>
    </div>
    <div v-else v-loading="true" class="h-40" />
  </el-drawer>
</template>

<style scoped>
.meta {
  padding: 8px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-fill-color-light);
}
.meta-label {
  margin-bottom: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.block-label {
  margin-bottom: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.code-block,
.prompt :deep(pre) {
  max-height: 24rem;
  margin: 0;
  padding: 8px 12px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-family: var(--ap-font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.prompt {
  font-size: 13px;
}
/* 标题里的参数预览过长时截断，不把右侧箭头挤出去 */
:deep(.el-collapse-item__title) {
  flex: 1;
  min-width: 0;
}
.prompt :deep(pre) {
  margin: 6px 0 10px;
}
.prompt :deep(h2) {
  margin-top: 1.2em;
  font-size: 1.05em;
}
.prompt :deep(h3) {
  font-size: 1em;
}
</style>
