<script setup lang="ts">
import type { Analysis } from "@@/apis/briefings"
import { getAnalysisApi, retryAnalysisApi, states } from "@@/apis/briefings"
import { useSessionScope } from "@@/composables/useSessionScope"

const props = defineProps<{ id: number | null }>()
const emit = defineEmits<{ version: [id: number], changed: [] }>()
const scope = useSessionScope()
const detail = ref<Analysis>()
const failed = ref(false)
const busy = ref(false)
let sequence = 0
async function load() {
  const seq = ++sequence
  detail.value = undefined
  failed.value = false
  if (props.id == null) return
  try {
    const result = await getAnalysisApi(props.id)
    if (scope.isCurrent() && seq === sequence) detail.value = result
  } catch {
    if (scope.isCurrent() && seq === sequence) failed.value = true
  }
}
async function retry() {
  if (!detail.value || busy.value) return
  const id = detail.value.id
  busy.value = true
  try {
    await ElMessageBox.confirm(
      "将使用当前原文创建新的分析版本，保留旧结果。确定重新分析？",
      "重新分析",
      { type: "warning" }
    )
  } catch {
    if (scope.isCurrent()) busy.value = false
    return
  }
  if (!scope.isCurrent() || props.id !== id) {
    if (scope.isCurrent()) busy.value = false
    return
  }
  try {
    const result = await retryAnalysisApi(id)
    if (scope.isCurrent()) {
      emit("version", result.id)
      emit("changed")
    }
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
watch(() => props.id, load, { immediate: true })
</script>

<template>
  <div v-if="detail" class="analysis-detail">
    <h2>{{ detail.title }}</h2>
    <p>
      <el-tag>{{ states[detail.state] ?? detail.state }}</el-tag> · 分析 v{{
        detail.version_no
      }}
      · 原文修订 {{ detail.source_revision ?? "未冻结" }} ·
      {{ detail.lane === "historical_backfill" ? "历史回补" : "实时新闻" }}
    </p>
    <el-alert
      v-if="detail.source_changed"
      title="分析对应旧版原文；来源更新不会自动重算，可手动创建新版本。"
      type="warning"
      :closable="false"
    />
    <el-alert
      v-if="detail.error"
      :title="detail.error"
      type="error"
      :closable="false"
    />
    <p>
      {{
        detail.time_basis === "first_seen_at"
          ? "按入站时间归窗"
          : "按发布时间归窗"
      }}
      ·
      {{
        new Date(detail.effective_at).toLocaleString("zh-CN", {
          timeZone: "Asia/Shanghai",
        })
      }}
    </p>
    <div class="actions">
      <router-link :to="{ path: '/news', query: { news: detail.news_id } }">
        查看新闻与原文修订
      </router-link>
      <el-button
        :disabled="['pending', 'processing'].includes(detail.state)"
        :loading="busy"
        @click="retry"
      >
        重新分析
      </el-button>
      <el-select
        :model-value="detail.id"
        aria-label="分析版本"
        @change="emit('version', Number($event))"
      >
        <el-option
          v-for="version in detail.versions ?? []"
          :key="version.id"
          :value="version.id"
          :label="`v${version.version_no} · ${states[version.state] ?? version.state}`"
        />
      </el-select>
    </div>
    <template v-if="detail.result">
      <p>
        <b>{{ "★".repeat(detail.importance_stars ?? 0) }}
          {{ detail.importance_score }}/100</b>
        · {{ detail.result.importance_reason }}
      </p>
      <h3>摘要</h3>
      <p>{{ detail.result.summary }}</p>
      <h3>关键词</h3>
      <div class="actions">
        <el-tag
          v-for="word in detail.keywords"
          :key="word.canonical"
          :title="`${word.aliases.join(' / ')} · 证据：${word.quote}`"
        >
          {{ word.canonical }}
        </el-tag>
      </div>
      <p>
        板块：{{ detail.result.primary_region.name }} ·
        {{
          detail.result.related_regions.map((region) => region.name).join("、")
        }}
      </p>
      <h3>报道事实</h3>
      <div v-for="fact in detail.result.facts" :key="fact.text">
        <p>{{ fact.text }}</p>
        <blockquote>{{ fact.quote }}</blockquote>
      </div>
      <h3>影响判断</h3>
      <p>{{ detail.result.impact }}</p>
      <h3>未确定信息</h3>
      <p v-for="text in detail.result.uncertainties" :key="text">
        {{ text }}
      </p>
      <h3>条件情景</h3>
      <p v-for="scenario in detail.result.scenarios" :key="scenario.condition">
        如果 {{ scenario.condition }} → {{ scenario.consequence }}
      </p>
    </template>
    <details>
      <summary>模型、输入、规则与用量审计</summary>
      <pre>{{
        JSON.stringify(
          { input: detail.input, rules: detail.rules, usage: detail.usage },
          null,
          2,
        )
      }}</pre>
    </details>
  </div>
  <el-result v-else-if="failed" icon="error" title="分析读取失败">
    <template #extra>
      <el-button @click="load">
        重试读取
      </el-button>
    </template>
  </el-result>
  <el-empty v-else-if="id == null" description="请选择分析" />
  <p v-else>
    正在读取分析…
  </p>
</template>

<style scoped>
.analysis-detail {
  display: grid;
  gap: 12px;
  overflow-wrap: anywhere;
}
p {
  line-height: 1.8;
  margin: 0;
  white-space: pre-wrap;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
.actions .el-select {
  width: 220px;
}
blockquote {
  color: var(--el-text-color-secondary);
  border-left: 3px solid var(--el-border-color);
  padding-left: 12px;
  margin: 8px 0;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
</style>
