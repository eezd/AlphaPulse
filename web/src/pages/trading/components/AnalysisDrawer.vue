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
      <div class="text-xs text-[var(--el-text-color-secondary)]">
        {{ formatTime(detail.started_at) }} – {{ formatTime(detail.finished_at) }} · {{ detail.reasons.join("；") }}
        · {{ detail.model }}{{ detail.reasoning_effort ? ` / ${detail.reasoning_effort}` : "" }}
        · {{ detail.usage?.input_tokens.toLocaleString() }} / {{ detail.usage?.output_tokens.toLocaleString() }} token
        {{ detail.dry_run ? "· 试运行" : "" }}
      </div>
      <el-alert v-if="detail.error" type="error" :closable="false" :title="detail.error" />
      <!-- markdown-it 已关闭 html，模型输出中的标签按文本显示 -->
      <div v-if="detail.text" class="markdown-body" v-html="renderMarkdown(detail.text)" />
      <el-collapse>
        <template v-for="(m, i) in detail.transcript ?? []" :key="i">
          <el-collapse-item v-if="m.role === 'user'" :name="`u${i}`" :title="i === 0 ? '发给模型的数据（账户、持仓、预取行情）' : '程序提示'">
            <pre class="m-0 max-h-120 overflow-auto whitespace-pre-wrap break-all text-xs">{{ m.text }}</pre>
          </el-collapse-item>
          <template v-else-if="m.role === 'assistant'">
            <el-collapse-item v-for="call in m.tool_calls" :key="call.id" :name="call.id">
              <template #title>
                <span class="inline-block max-w-[90%] truncate text-xs font-mono">{{ call.name }} {{ JSON.stringify(call.arguments) }}</span>
              </template>
              <pre class="m-0 max-h-120 overflow-auto whitespace-pre-wrap break-all text-xs">{{ pretty(toolResults.get(call.id)) }}</pre>
            </el-collapse-item>
          </template>
        </template>
      </el-collapse>
    </div>
    <div v-else v-loading="true" class="h-40" />
  </el-drawer>
</template>
