<script setup lang="ts">
import type { ReportDetail } from "@@/apis/briefings"
import { getReportApi, kinds, states } from "@@/apis/briefings"
import { useSessionScope } from "@@/composables/useSessionScope"

const props = defineProps<{ id: number | null }>()
const emit = defineEmits<{ version: [id: number] }>()
const scope = useSessionScope()
const detail = ref<ReportDetail>()
const failed = ref(false)
let sequence = 0
function date(value: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
    : "—"
}
async function load() {
  const seq = ++sequence
  detail.value = undefined
  failed.value = false
  if (props.id == null) return
  try {
    const data = await getReportApi(props.id)
    if (scope.isCurrent() && seq === sequence) detail.value = data
  } catch {
    if (scope.isCurrent() && seq === sequence) failed.value = true
  }
}
watch(() => props.id, load, { immediate: true })
</script>

<template>
  <article v-if="detail" class="report-detail">
    <h1>
      {{
        detail.output?.title ?? `${detail.report_date} ${kinds[detail.kind]}`
      }}
    </h1>
    <div class="actions">
      <el-tag>{{ states[detail.state] ?? detail.state }}</el-tag><el-tag v-if="detail.lane === 'historical_backfill'" type="warning">
        历史回放
      </el-tag><span>归属 {{ detail.report_date }} · v{{ detail.version_no }}</span><el-select
        :model-value="detail.id"
        aria-label="报告版本"
        @change="emit('version', Number($event))"
      >
        <el-option
          v-for="version in detail.versions"
          :key="version.id"
          :value="version.id"
          :label="`v${version.version_no} · ${states[version.state] ?? version.state}`"
        />
      </el-select><el-button @click="load">
        刷新详情
      </el-button>
    </div>
    <p>
      覆盖 {{ date(detail.window_start) }} —
      {{ date(detail.window_end) }}（北京时间，左闭右开）
    </p>
    <p>
      资料截止 {{ date(detail.cutoff_at) }} · 实际生成
      {{ date(detail.finished_at) }}
    </p>
    <el-alert
      v-if="detail.lane === 'historical_backfill'"
      type="info"
      :closable="false"
      title="历史回放使用当前留存资料，不代表当时已发布或当时可知的判断。"
    />
    <el-alert
      v-if="detail.error"
      type="error"
      :title="detail.error"
      :closable="false"
    />
    <el-alert
      v-if="
        detail.state === 'waiting_coverage'
          || detail.state === 'waiting_backfill'
      "
      type="warning"
      :closable="false"
      :title="`等待资料：${detail.coverage?.reason ?? states[detail.state]}；下次检查 ${date(detail.next_check_at)}`"
    />
    <p v-if="detail.coverage">
      新闻 {{ detail.coverage.total ?? "—" }} · 可用
      {{ detail.coverage.available ?? "—" }} · 缺失
      {{ detail.coverage.missing ?? "—" }}（{{
        ((detail.coverage.missing_rate ?? 0) * 100).toFixed(2)
      }}%） {{ detail.coverage.reason }}
    </p>
    <div v-if="detail.input?.dependencies?.length" class="actions">
      复用分时报告：<router-link
        v-for="dependency in detail.input.dependencies"
        :key="dependency.report_id"
        :to="{ path: '/reports', query: { report: dependency.report_id } }"
      >
        {{ kinds[dependency.kind] }} v{{ dependency.version_no }}
      </router-link>
    </div>
    <p>{{ detail.output?.summary }}</p>
    <section
      v-for="section in detail.output?.sections ?? []"
      :key="section.region.code"
      class="region"
    >
      <h2>{{ section.region.name }}</h2>
      <p>{{ section.summary }}</p>
      <el-card
        v-for="event in section.events"
        :key="event.title"
        shadow="never"
      >
        <h3>
          {{ event.title }} <small>{{ event.importance_score }}/100</small>
        </h3>
        <h4>报道事实</h4>
        <ul>
          <li v-for="text in event.facts" :key="text">
            {{ text }}
          </li>
        </ul>
        <h4 v-if="event.market_changes.length">
          标的与市场表现
        </h4>
        <p v-for="text in event.market_changes" :key="text">
          {{ text }}
        </p>
        <h4>影响判断</h4>
        <p>{{ event.impact }}</p>
        <h4 v-if="event.suggestions.length">
          关注建议
        </h4>
        <p v-for="text in event.suggestions" :key="text">
          {{ text }}
        </p>
        <h4 v-if="event.watch_points.length">
          后续观察
        </h4>
        <p v-for="text in event.watch_points" :key="text">
          {{ text }}
        </p>
        <h4 v-if="event.scenarios.length">
          条件情景
        </h4>
        <p v-for="scenario in event.scenarios" :key="scenario.condition">
          如果 {{ scenario.condition }} → {{ scenario.consequence }}
        </p>
        <h4 v-if="event.uncertainties.length">
          未确定信息
        </h4>
        <p v-for="text in event.uncertainties" :key="text">
          {{ text }}
        </p>
        <div class="actions">
          来源分析：<router-link
            v-for="analysisId in event.source_analysis_ids"
            :key="analysisId"
            :to="{ path: '/home', query: { analysis: analysisId } }"
          >
            #{{ analysisId }}
          </router-link>
        </div>
      </el-card>
    </section>
    <h2>窗口行情</h2>
    <p>
      来自已加入行情的标的，按窗口首根开盘至末根收盘计算。股票变化仅采用新闻所引数据。
    </p>
    <div
      v-for="market in detail.market ?? []"
      :key="`${market.market}:${market.symbol}`"
    >
      <b>{{ market.symbol }} ·
        {{ market.market === "spot" ? "币安现货" : "币安永续合约" }}</b>
      <p v-if="market.status === 'complete'">
        {{ market.return_pct }}% · 首价 {{ market.first_open }} · 末价
        {{ market.last_close }}
      </p>
      <p v-else>
        数据不足，未计算窗口收益率。
      </p>
    </div>
    <p v-if="!detail.market?.length">
      未配置标的或无可用行情。
    </p>
    <h2>原文与固定分析版本</h2>
    <div
      v-for="source in detail.sources"
      :key="source.analysis_id"
      class="source"
    >
      <router-link
        :to="{ path: '/home', query: { analysis: source.analysis_id } }"
      >
        {{ source.title }} · 分析 #{{ source.analysis_id }} / 原文修订
        {{ source.source_revision }}
      </router-link><router-link :to="{ path: '/news', query: { news: source.news_id } }">
        查看新闻修订
      </router-link><a
        v-if="source.url"
        :href="source.url"
        target="_blank"
        rel="noopener noreferrer"
      >外部原文</a>
    </div>
    <h2>生成步骤与用量</h2>
    <p>
      已知输入 {{ detail.usage.input_tokens }} Token，输出
      {{ detail.usage.output_tokens }}；未知用量调用
      {{ detail.usage.unknown_calls }}，复用步骤
      {{ detail.usage.reused_steps }}。
    </p>
    <p v-for="part in detail.parts" :key="part.id">
      {{ part.part_key }} · {{ states[part.state] ?? part.state }}
      {{ part.reused_from ? `复用步骤 #${part.reused_from}` : "" }}
      {{ part.error }}
    </p>
    <details>
      <summary>规则、覆盖率、输入清单审计</summary>
      <pre>{{
        JSON.stringify(
          {
            rules: detail.rules,
            coverage: detail.coverage,
            input: detail.input,
          },
          null,
          2,
        )
      }}</pre>
    </details>
  </article>
  <el-result v-else-if="failed" icon="error" title="报告读取失败">
    <template #extra>
      <el-button @click="load">
        重试读取
      </el-button>
    </template>
  </el-result>
  <p v-else>
    正在读取报告…
  </p>
</template>

<style scoped>
.report-detail,
.region {
  display: grid;
  gap: 14px;
  overflow-wrap: anywhere;
}
p,
li {
  line-height: 1.8;
  white-space: pre-wrap;
}
h1,
h2,
h3,
h4,
p {
  margin: 6px 0;
}
.actions,
.source {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.actions .el-select {
  width: 200px;
}
pre {
  white-space: pre-wrap;
  font-size: 12px;
}
</style>
