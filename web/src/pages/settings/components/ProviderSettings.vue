<script setup lang="ts">
import type { Provider, ProviderForm } from "@@/apis/settings/type"
import type { FormInstance, FormRules } from "element-plus"
import { createProviderApi, deleteProviderApi, listProvidersApi, patchProviderApi, testProviderApi } from "@@/apis/settings"
import { useSessionScope } from "@@/composables/useSessionScope"
import { EFFORTS } from "@@/constants/ai"
import { formatTime } from "@@/utils/text"
import { Plus } from "@element-plus/icons-vue"

const sessionScope = useSessionScope()

const PROTOCOL_LABELS = { openai: "OpenAI 格式（/v1/chat/completions）", anthropic: "Claude 格式（/v1/messages）" } as const

const providers = ref<Provider[]>([])
const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const editingDefault = ref(false)
const saving = ref(false)
const testing = ref<number | null>(null)

function emptyForm(): ProviderForm {
  return { name: "", protocol: "anthropic", base_url: "", api_key: "", model: "", max_tokens: 8192, context_window: 200000, reasoning_effort: null, is_default: false }
}
const form = ref<ProviderForm>(emptyForm())
const formRef = ref<FormInstance>()
const rules: FormRules = {
  name: [{ required: true, whitespace: true, message: "请输入名称", trigger: "blur" }],
  base_url: [{ validator: (_rule, value, callback) => {
    const message = "请输入 HTTP 或 HTTPS 地址"
    let url: URL
    try {
      url = new URL(value)
    } catch {
      callback(new Error(message))
      return
    }
    callback(["http:", "https:"].includes(url.protocol) ? undefined : new Error(message))
  }, trigger: "blur" }],
  api_key: [{ validator: (_rule, value, callback) => callback(editingId.value === null && !value?.trim() ? new Error("请输入 API Key") : undefined), trigger: "blur" }],
  model: [{ required: true, whitespace: true, message: "请输入模型名称", trigger: "blur" }],
  max_tokens: [{ required: true, type: "integer", min: 256, max: 128000, message: "最大输出须为 256–128000 的整数", trigger: "blur" }],
  context_window: [{ required: true, type: "integer", min: 4096, max: 2000000, message: "上下文上限须为 4096–2000000 的整数", trigger: "blur" }]
}
const loading = ref(false)
const failed = ref(false)

async function load() {
  loading.value = true
  try {
    providers.value = await listProvidersApi()
    failed.value = false
  } catch (error) {
    failed.value = true
    throw error
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  editingDefault.value = false
  form.value = emptyForm()
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

function openEdit(p: Provider) {
  editingId.value = p.id
  editingDefault.value = p.is_default
  // key 不回显，留空表示不修改
  form.value = {
    name: p.name,
    protocol: p.protocol,
    base_url: p.base_url,
    api_key: "",
    model: p.model,
    max_tokens: p.max_tokens,
    context_window: p.context_window,
    reasoning_effort: p.reasoning_effort,
    is_default: p.is_default
  }
  formRef.value?.clearValidate()
  dialogVisible.value = true
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    await formRef.value!.validate()
    if (editingId.value === null) {
      await createProviderApi(form.value)
    } else {
      const { api_key, ...rest } = form.value
      await patchProviderApi(editingId.value, api_key.trim() ? form.value : rest)
    }
    ElMessage.success("已保存")
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function setDefault(p: Provider) {
  await patchProviderApi(p.id, { is_default: true })
  await load()
}

async function remove(p: Provider) {
  await ElMessageBox.confirm(`删除模型配置「${p.name}」？`, "删除确认", { type: "warning" })
  sessionScope.assertCurrent()
  await deleteProviderApi(p.id)
  ElMessage.success("已删除")
  await load()
}

async function test(p: Provider) {
  testing.value = p.id
  try {
    const result = await testProviderApi(p.id)
    if (result.ok)
      ElMessage.success(`连接成功，用时 ${result.elapsed_ms}ms，回复：${result.reply}`)
    else
      ElMessage.error({ message: `连接失败（${result.elapsed_ms}ms）：${result.error}`, duration: 8000 })
  } finally {
    testing.value = null
  }
}

onMounted(load)
</script>

<template>
  <section class="min-w-0">
    <el-card shadow="never" body-class="p-0">
      <template #header>
        <div class="flex items-center justify-between">
          <div>
            模型配置
            <span class="ml-3 text-xs text-slate-500 font-normal">通过 Sub2API 等中转接入；API key 加密存储，页面只显示末 4 位</span>
          </div>
          <el-button type="primary" :icon="Plus" @click="openCreate">
            新增配置
          </el-button>
        </div>
      </template>
      <el-alert v-if="failed" type="error" :closable="false" title="模型配置读取失败">
        <el-button link @click="load">
          重试
        </el-button>
      </el-alert>
      <el-table v-loading="loading" :data="providers" :empty-text="failed ? '读取失败' : '暂无模型配置'">
        <el-table-column label="名称" min-width="120">
          <template #default="{ row }">
            {{ row.name }}
            <el-tag v-if="row.is_default" size="small" type="success" class="ml-1">
              默认
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="协议" min-width="200">
          <template #default="{ row }">
            {{ PROTOCOL_LABELS[row.protocol as keyof typeof PROTOCOL_LABELS] }}
          </template>
        </el-table-column>
        <el-table-column prop="base_url" label="Base URL" min-width="200" />
        <el-table-column prop="model" label="模型" min-width="150" />
        <el-table-column label="思考强度" width="90">
          <template #default="{ row }">
            {{ (row as Provider).reasoning_effort ?? "-" }}
          </template>
        </el-table-column>
        <el-table-column label="API Key" width="100">
          <template #default="{ row }">
            ****{{ row.api_key_hint }}
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="130">
          <template #default="{ row }">
            {{ formatTime(row.updated_at, "MM-DD HH:mm") }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="testing !== null || saving" :loading="testing === row.id" @click="test(row as Provider)">
              测试连接
            </el-button>
            <el-button link @click="openEdit(row as Provider)">
              编辑
            </el-button>
            <el-button v-if="!row.is_default" link @click="setDefault(row as Provider)">
              设为默认
            </el-button>
            <el-button link type="danger" @click="remove(row as Provider)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editingId === null ? '新增模型配置' : '编辑模型配置'" width="min(560px, 94vw)" :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving">
      <el-form ref="formRef" :model="form" :rules="rules" :disabled="saving" label-width="100px">
        <el-form-item prop="name" label="名称" required>
          <el-input v-model="form.name" placeholder="如 Sub2API-Claude" />
        </el-form-item>
        <el-form-item label="协议" required>
          <el-radio-group v-model="form.protocol">
            <el-radio value="anthropic">
              Claude 格式
            </el-radio>
            <el-radio value="openai">
              OpenAI 格式
            </el-radio>
          </el-radio-group>
          <div class="text-xs text-[var(--el-text-color-secondary)]">
            选 key 所在分组的原生协议；Sub2API 的协议转换会丢失部分信息
          </div>
        </el-form-item>
        <el-form-item prop="base_url" label="Base URL" required>
          <el-input v-model="form.base_url" placeholder="https://your-sub2api.example.com（带不带 /v1 都可以）" />
        </el-form-item>
        <el-form-item prop="api_key" label="API Key" :required="editingId === null">
          <el-input v-model="form.api_key" type="password" show-password autocomplete="new-password" :placeholder="editingId === null ? '' : '留空表示不修改'" />
        </el-form-item>
        <el-form-item prop="model" label="模型" required>
          <el-input v-model="form.model" placeholder="如 claude-sonnet-4-5" />
        </el-form-item>
        <el-form-item prop="max_tokens" label="最大输出">
          <el-input-number v-model="form.max_tokens" :min="256" :max="128000" :step="1024" controls-position="right" />
          <span class="ml-2 text-xs">token</span>
        </el-form-item>
        <el-form-item prop="context_window" label="上下文上限">
          <el-input-number v-model="form.context_window" :min="4096" :max="2000000" :step="10000" controls-position="right" />
          <span class="ml-2 text-xs">token，超过时拒绝发送</span>
        </el-form-item>
        <el-form-item label="思考强度">
          <el-select v-model="form.reasoning_effort" clearable placeholder="不设置" class="w-40" :value-on-clear="null">
            <el-option v-for="e in EFFORTS" :key="e" :value="e" :label="e" />
          </el-select>
          <span class="ml-2 text-xs">Chat 默认值，可在 Chat 页按轮调整；留空则不传，由模型决定。越高越慢、越贵</span>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" :disabled="editingDefault" />
          <span v-if="editingDefault" class="ml-2 text-xs">请将其他模型设为默认以切换</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="dialogVisible = false">
          取消
        </el-button>
        <el-button type="primary" :loading="saving" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
