<script lang="ts" setup>
import type { AnalysisDetail } from "@/apis/trading"
import { Cpu } from "@element-plus/icons-vue"
import { getAnalysis } from "@/apis/trading"
import { renderMarkdown } from "@/utils/markdown"
import { formatDuration, formatTime } from "@/utils/text"

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
  <el-drawer v-model="visible" size="55%">
    <template #header>
      <div class="min-w-0 flex items-center gap-2">
        <el-icon class="text-ap-brand" :size="18">
          <Cpu />
        </el-icon>
        <div class="min-w-0 flex flex-col">
          <span class="font-head text-sm text-ap-text font-bold">{{ detail ? `分析 #${detail.id} · ${detail.symbol}` : "分析" }}</span>
          <span v-if="detail" class="truncate text-[10px] text-ap-muted">
            {{ formatTime(detail.started_at) }} – {{ formatTime(detail.finished_at) }} · {{ detail.reasons.join("；") }}{{ detail.dry_run ? " · 试运行" : "" }}
          </span>
        </div>
      </div>
    </template>
    <div v-if="detail" class="flex flex-col gap-3 text-sm">
      <div class="grid grid-cols-4 gap-2 border border-ap-line border-solid bg-ap-sunken p-3 text-xs">
        <div>
          <div class="text-[10px] text-ap-muted">
            分析 ID
          </div>
          <b>#{{ detail.id }}</b>
        </div>
        <div>
          <div class="text-[10px] text-ap-muted">
            耗时
          </div>
          <b class="text-ap-brand">{{ detail.finished_at ? formatDuration(detail.started_at, detail.finished_at) : "-" }}</b>
        </div>
        <div>
          <div class="text-[10px] text-ap-muted">
            token 输入 / 输出
          </div>
          <b>{{ detail.usage?.input_tokens.toLocaleString() ?? "-" }} / {{ detail.usage?.output_tokens.toLocaleString() ?? "-" }}</b>
        </div>
        <div>
          <div class="text-[10px] text-ap-muted">
            模型 / 思考强度
          </div>
          <b class="text-ap-up">{{ detail.model ?? "-" }}{{ detail.reasoning_effort ? ` / ${detail.reasoning_effort}` : "" }}</b>
        </div>
      </div>
      <el-alert v-if="detail.error" type="error" :closable="false" :title="detail.error" />
      <el-card v-if="detail.text" shadow="never" class="bg-ap-panel-low!">
        <template #header>
          模型分析
        </template>
        <!-- markdown-it 已关闭 html，模型输出中的标签按文本显示 -->
        <div class="markdown-body" v-html="renderMarkdown(detail.text)" />
      </el-card>
      <el-card shadow="never" class="bg-ap-panel-low!" body-class="p-0!">
        <template #header>
          输入数据与工具调用 ({{ toolResults.size }})
        </template>
        <el-collapse>
          <template v-for="(m, i) in detail.transcript ?? []" :key="i">
            <el-collapse-item v-if="m.role === 'user'" :name="`u${i}`" :title="i === 0 ? '发给模型的数据（账户、持仓、预取行情）' : '程序提示'">
              <pre class="m-0 max-h-120 overflow-auto whitespace-pre-wrap break-all text-xs">{{ m.text }}</pre>
            </el-collapse-item>
            <template v-else-if="m.role === 'assistant'">
              <el-collapse-item v-for="call in m.tool_calls" :key="call.id" :name="call.id">
                <template #title>
                  <span class="text-xs">
                    <b class="text-ap-up">{{ call.name }}</b>
                    <span class="ml-2 text-ap-muted">{{ JSON.stringify(call.arguments) }}</span>
                  </span>
                </template>
                <pre class="m-0 max-h-120 overflow-auto whitespace-pre-wrap break-all text-xs">{{ pretty(toolResults.get(call.id)) }}</pre>
              </el-collapse-item>
            </template>
          </template>
        </el-collapse>
      </el-card>
    </div>
    <div v-else v-loading="true" class="h-40" />
  </el-drawer>
</template>
