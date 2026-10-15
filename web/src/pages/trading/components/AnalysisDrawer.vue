<script lang="ts" setup>
import type { AnalysisDetail } from "@/apis/trading"
import { getAnalysis } from "@/apis/trading"
import { renderMarkdown } from "@/utils/markdown"
import { formatTime } from "@/utils/text"

const props = defineProps<{ analysisId: number | null }>()
const visible = defineModel<boolean>({ required: true })

const detail = ref<AnalysisDetail>()

watch(() => [visible.value, props.analysisId] as const, async ([open, id]) => {
  detail.value = undefined
  if (open && id)
    detail.value = await getAnalysis(id)
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

function pretty(raw: string | undefined) {
  if (raw === undefined)
    return "无结果"
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
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
              <pre class="m-0 max-h-120 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 text-xs">{{ m.text }}</pre>
            </el-collapse-item>
            <template v-else-if="m.role === 'assistant'">
              <el-collapse-item v-for="call in m.tool_calls" :key="call.id" :name="call.id">
                <template #title>
                  <span class="inline-block max-w-[90%] truncate text-xs font-mono">{{ call.name }} {{ JSON.stringify(call.arguments) }}</span>
                </template>
                <pre class="m-0 max-h-120 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 text-xs">{{ pretty(toolResults.get(call.id)) }}</pre>
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
</style>
