<script setup lang="ts">
import type {
  BriefingConfig,
  QueueCoverage,
  Settings
} from "@@/apis/briefings"
import type { Provider } from "@@/apis/settings/type"
import type { SkillNode } from "@@/apis/skills/type"
import {
  enableApi,
  getCoverageApi,
  getSettingsApi,
  getSkillTemplatesApi,
  initializeSkillTemplatesApi,
  pauseApi,
  saveSettingsApi,
  saveTelegramApi
} from "@@/apis/briefings"
import { listProviderModelsApi, listProvidersApi } from "@@/apis/settings"
import { listSkillTreeApi } from "@@/apis/skills"
import { useSessionScope } from "@@/composables/useSessionScope"

const emit = defineEmits<{ changed: [] }>()
const scope = useSessionScope()
const settings = ref<Settings>()
const coverage = ref<QueueCoverage>()
const providers = ref<Provider[]>([])
const skills = ref<SkillNode[]>([])
const busy = ref(false)
const failed = ref(false)
const tg = reactive({
  enabled: false,
  token: "",
  chat_id: "",
  clear_token: false
})
const form = reactive<BriefingConfig>({
  concurrency: 3,
  backfill_start: null,
  analysis: {
    provider_id: null,
    model: "",
    skill_id: null,
    reasoning_effort: "low",
    max_tokens: 4096
  },
  period: {
    provider_id: null,
    model: "",
    skill_id: null,
    reasoning_effort: "medium",
    max_tokens: 8192
  },
  daily: {
    provider_id: null,
    model: "",
    skill_id: null,
    reasoning_effort: "medium",
    max_tokens: 8192
  }
})
const stages = [
  { key: "analysis", name: "单篇新闻分析" },
  { key: "period", name: "分时报告" },
  { key: "daily", name: "日报汇总" }
] as const
type Stage = (typeof stages)[number]["key"]
const catalogs = reactive<Record<number, {
  items: string[]
  loading: boolean
  loaded: boolean
  error: string
}>>({})
let sequence = 0
async function load() {
  if (!scope.isCurrent()) return
  const seq = ++sequence
  try {
    const result = await Promise.all([
      getSettingsApi(),
      getCoverageApi(),
      listProvidersApi(),
      listSkillTreeApi()
    ])
    if (!scope.isCurrent() || seq !== sequence) return
    settings.value = result[0]
    coverage.value = result[1]
    providers.value = result[2]
    skills.value = result[3].filter(
      skill =>
        skill.scope === "news"
        && skill.enabled
        && skill.kind === "skill"
        && skill.published_version_no
    )
    for (const { key } of stages)
      Object.assign(form[key], result[0].config[key] ?? {})
    form.concurrency = result[0].config.concurrency ?? 3
    form.backfill_start = result[0].config.backfill_start ?? null
    tg.enabled = result[0].telegram.enabled
    failed.value = false
    await Promise.all(stages.map(({ key }) => loadModels(form[key].provider_id)))
  } catch {
    if (scope.isCurrent() && seq === sequence) failed.value = true
  }
}
async function loadModels(providerId: number | null, force = false) {
  if (!scope.isCurrent() || providerId === null) return
  const previous = catalogs[providerId]
  if (!force && (previous?.loading || previous?.loaded)) return
  const entry = reactive({ items: previous?.items ?? [], loading: true, loaded: false, error: "" })
  catalogs[providerId] = entry
  try {
    const result = await listProviderModelsApi(providerId)
    if (!scope.isCurrent() || catalogs[providerId] !== entry) return
    entry.items = result
    entry.loaded = true
    if (!result.length) entry.error = "此接入未返回可选模型，请检查模型接入配置"
  } catch {
    if (scope.isCurrent() && catalogs[providerId] === entry)
      entry.error = "模型列表读取失败，请重试或检查模型接入配置"
  } finally {
    if (scope.isCurrent() && catalogs[providerId] === entry) entry.loading = false
  }
}
function catalog(stage: Stage) {
  const id = form[stage].provider_id
  return id === null ? undefined : catalogs[id]
}
async function providerChanged(stage: Stage) {
  const id = form[stage].provider_id
  form[stage].model = ""
  await loadModels(id)
  if (!scope.isCurrent() || form[stage].provider_id !== id || form[stage].model) return
  const provider = providers.value.find(item => item.id === id)
  if (provider && catalog(stage)?.items.includes(provider.model))
    form[stage].model = provider.model
}
async function initializeTemplates() {
  if (!scope.isCurrent() || !settings.value || busy.value) return
  const revision = settings.value.control_revision
  const seq = sequence
  busy.value = true
  try {
    await ElMessageBox.confirm(
      "创建并发布缺少的三份新闻 Skill，不覆盖已有内容。自动填入未配置的位置，保存配置后才会绑定；不会启用分析或回补。",
      "初始SKILL模板",
      { type: "warning" }
    )
  } catch {
    if (scope.isCurrent()) busy.value = false
    return
  }
  if (!scope.isCurrent()) return
  if (seq !== sequence || settings.value?.control_revision !== revision) {
    busy.value = false
    return
  }
  try {
    const result = await initializeSkillTemplatesApi(revision)
    if (!scope.isCurrent() || seq !== sequence) return
    const tree = await listSkillTreeApi()
    if (!scope.isCurrent() || seq !== sequence) return
    skills.value = tree.filter(skill => skill.scope === "news" && skill.enabled
      && skill.kind === "skill" && skill.published_version_no)
    for (const item of result) {
      if (form[item.stage].skill_id === null) form[item.stage].skill_id = item.skill_id
    }
    ElMessage.success("新闻 Skill 已就绪，请核对模型和回补日期后保存配置")
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
async function save() {
  if (!scope.isCurrent() || !settings.value || busy.value) return
  const body = JSON.parse(
    JSON.stringify({ ...form, base_revision: settings.value.control_revision })
  )
  busy.value = true
  try {
    await saveSettingsApi(body)
    if (scope.isCurrent()) {
      await load()
      emit("changed")
    }
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
async function control() {
  if (!scope.isCurrent() || !settings.value || busy.value) return
  const enabled = settings.value.enabled
  const revision = settings.value.control_revision
  busy.value = true
  try {
    await ElMessageBox.confirm(
      enabled
        ? "暂停并中断在途分析及报告？已完成版本会保留。"
        : `启用后将从 ${settings.value.config.backfill_start ?? "最早新闻日期"} 的北京时间 00:00 起统一回补单篇分析、分时报告和日报，并处理新新闻。确认开启？`,
      enabled ? "暂停处理" : "启用新流程",
      { type: "warning" }
    )
  } catch {
    if (scope.isCurrent()) busy.value = false
    return
  }
  if (!scope.isCurrent()) return
  if (
    !settings.value
    || settings.value.control_revision !== revision
    || settings.value.enabled !== enabled
  ) {
    busy.value = false
    return
  }
  try {
    await (enabled ? pauseApi : enableApi)(revision)
    if (scope.isCurrent()) {
      await load()
      emit("changed")
    }
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
async function saveTelegram() {
  if (!scope.isCurrent() || !settings.value || busy.value) return
  busy.value = true
  try {
    await saveTelegramApi({
      enabled: tg.enabled,
      base_revision: settings.value.control_revision,
      clear_token: tg.clear_token,
      ...(tg.token ? { token: tg.token } : {}),
      ...(tg.chat_id ? { chat_id: tg.chat_id } : {})
    })
    if (scope.isCurrent()) {
      tg.token = ""
      tg.chat_id = ""
      tg.clear_token = false
      await load()
    }
  } finally {
    if (scope.isCurrent()) busy.value = false
  }
}
async function copyTemplate(stage: string) {
  const templates = await getSkillTemplatesApi()
  if (!scope.isCurrent()) return
  const template = templates.find(item => item.stage === stage)
  if (template) {
    await navigator.clipboard.writeText(template.content)
    ElMessage.success(
      "已复制模板；请在 Skills 创建新闻专用 Skill 并发布后选择"
    )
  }
}
onMounted(load)
</script>

<template>
  <div class="settings-grid">
    <el-alert
      v-if="failed"
      type="error"
      title="配置读取失败，请刷新后重试"
      :closable="false"
    />
    <p>
      北京时间：早报 09:05、晚报 18:05、盘中报 00:05、日报 08:05
      开始判断。每条新闻自动分析一次，原文更新后手动重做。
    </p>
    <p v-if="coverage">
      范围内新闻 {{ coverage.news_total }} 条，尚未入队
      {{ coverage.not_enrolled }} 条；历史回补范围共
      {{ coverage.historical_windows_expected }} 个窗口。回补
      {{ coverage.history_start }} 至 {{ coverage.history_end }}，游标
      {{ coverage.history_cursor ?? "尚未启动" }}。
    </p>
    <el-button :disabled="!settings" :loading="busy" @click="initializeTemplates">
      初始SKILL模板
    </el-button>
    <el-form label-position="top" :disabled="busy">
      <el-form-item label="全局历史回补起始日期（北京时间）">
        <el-date-picker
          v-model="form.backfill_start"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="留空表示全部历史"
          clearable
        />
      </el-form-item>
      <p>从所选日期的北京时间 00:00 起，统一回补单篇新闻分析、分时报告和日报；留空表示全部历史。已有结果保留，手动重试和补报可处理其他日期。</p>
      <div class="stage-grid">
        <el-card v-for="stage in stages" :key="stage.key" shadow="never">
          <h3>{{ stage.name }}</h3>
          <el-form-item label="模型配置">
            <el-select
              v-model="form[stage.key].provider_id"
              @change="providerChanged(stage.key)"
            >
              <el-option
                v-for="provider in providers"
                :key="provider.id"
                :value="provider.id"
                :label="provider.name"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="模型名称">
            <el-select
              v-model="form[stage.key].model"
              filterable
              :disabled="!form[stage.key].provider_id || catalog(stage.key)?.loading"
              :loading="catalog(stage.key)?.loading"
              placeholder="从模型列表选择"
            >
              <el-option
                v-if="form[stage.key].model && !catalog(stage.key)?.items.includes(form[stage.key].model)"
                :value="form[stage.key].model"
                :label="`${form[stage.key].model}（当前配置）`"
                disabled
              />
              <el-option
                v-for="model in catalog(stage.key)?.items ?? []"
                :key="model"
                :value="model"
                :label="model"
              />
            </el-select>
            <el-button
              link
              :disabled="!form[stage.key].provider_id || catalog(stage.key)?.loading"
              @click="loadModels(form[stage.key].provider_id, true)"
            >
              刷新模型列表
            </el-button>
            <p v-if="catalog(stage.key)?.error" role="alert">
              {{ catalog(stage.key)?.error }}
            </p>
          </el-form-item>
          <el-form-item label="已发布的新闻 Skill">
            <el-select v-model="form[stage.key].skill_id" filterable>
              <el-option
                v-for="skill in skills"
                :key="skill.id"
                :value="skill.id"
                :label="`${skill.name} · v${skill.published_version_no}`"
              />
            </el-select>
          </el-form-item>
          <el-button link type="primary" @click="copyTemplate(stage.key)">
            复制此任务 Skill 模板
          </el-button>
          <el-form-item label="思考强度">
            <el-select v-model="form[stage.key].reasoning_effort" clearable>
              <el-option
                v-for="effort in ['low', 'medium', 'high', 'xhigh', 'max']"
                :key="effort"
                :value="effort"
                :label="effort"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="输出上限">
            <el-input-number
              v-model="form[stage.key].max_tokens"
              :min="1024"
              :max="32768"
            />
          </el-form-item>
        </el-card>
      </div>
      <el-form-item label="总并发">
        <el-input-number v-model="form.concurrency" :min="1" :max="16" />
      </el-form-item>
    </el-form>
    <div class="actions">
      <el-button
        type="primary"
        :disabled="!settings"
        :loading="busy"
        @click="save"
      >
        保存配置并暂停
      </el-button><el-button
        :disabled="!settings"
        :loading="busy"
        :type="settings?.enabled ? 'warning' : 'success'"
        @click="control"
      >
        {{
          settings?.enabled ? "暂停并中断" : "启用回补与实时处理"
        }}
      </el-button><el-button :disabled="busy" @click="load">
        刷新配置
      </el-button><router-link to="/skills">
        前往 Skills
      </router-link>
    </div>
    <h3>运行异常 Telegram 通知</h3>
    <p>
      仅发送缺失、阻塞和疑似上游异常；新闻星级及价格提醒后续开放。当前 token
      {{ settings?.telegram.token_configured ? "已配置" : "未配置" }}，会话
      {{ settings?.telegram.chat_id_masked ?? "未配置" }}。保存不发送测试消息。
    </p>
    <el-form label-position="top" autocomplete="off">
      <el-form-item label="启用异常通知">
        <el-switch v-model="tg.enabled" />
      </el-form-item>
      <el-form-item label="Bot token（留空保留）">
        <el-input
          v-model="tg.token"
          type="password"
          autocomplete="new-password"
        />
      </el-form-item>
      <el-form-item label="Chat ID（留空保留）">
        <el-input v-model="tg.chat_id" />
      </el-form-item>
      <el-checkbox v-model="tg.clear_token">
        清除已存 token
      </el-checkbox>
      <el-button :disabled="!settings" :loading="busy" @click="saveTelegram">
        保存异常通知
      </el-button>
    </el-form>
  </div>
</template>

<style scoped>
.settings-grid {
  display: grid;
  gap: 16px;
}
.stage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
  gap: 12px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
p {
  line-height: 1.8;
  overflow-wrap: anywhere;
}
</style>
