<script lang="ts" setup>
import type { Provider, ProviderForm } from "@/apis/settings"
import { createProvider, deleteProvider, listProviders, patchProvider, testProvider } from "@/apis/settings"
import { formatTime } from "@/utils/text"

const PROTOCOL_LABELS = { openai: "OpenAI 格式（/v1/chat/completions）", anthropic: "Claude 格式（/v1/messages）" } as const

const providers = ref<Provider[]>([])
const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const testing = ref<number | null>(null)

function emptyForm(): ProviderForm {
  return { name: "", protocol: "anthropic", base_url: "", api_key: "", model: "", max_tokens: 8192, context_window: 200000, temperature: null, is_default: false }
}
const form = ref<ProviderForm>(emptyForm())

async function load() {
  providers.value = await listProviders()
}

function openCreate() {
  editingId.value = null
  form.value = emptyForm()
  dialogVisible.value = true
}

function openEdit(p: Provider) {
  editingId.value = p.id
  // key 不回显，留空表示不修改
  form.value = { ...p, api_key: "" }
  dialogVisible.value = true
}

async function save() {
  saving.value = true
  try {
    if (editingId.value === null) {
      await createProvider(form.value)
    } else {
      const { api_key, ...rest } = form.value
      await patchProvider(editingId.value, api_key.trim() ? form.value : rest)
    }
    ElMessage.success("已保存")
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function setDefault(p: Provider) {
  await patchProvider(p.id, { is_default: true })
  await load()
}

async function remove(p: Provider) {
  await ElMessageBox.confirm(`删除模型配置「${p.name}」？`, "删除确认", { type: "warning" })
  await deleteProvider(p.id)
  ElMessage.success("已删除")
  await load()
}

async function test(p: Provider) {
  testing.value = p.id
  try {
    const result = await testProvider(p.id)
    if (result.ok)
      ElMessage.success(`连接成功，用时 ${result.elapsed_ms}ms，回复：${result.reply}`)
    else
      ElMessage.error({ message: `连接失败（${result.elapsed_ms}ms）：${result.error}`, duration: 8000 })
  } finally {
    testing.value = null
  }
}

load()
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="flex items-center justify-between">
      <div>
        <span class="text-lg font-bold">模型配置</span>
        <span class="ml-3 text-xs text-[var(--el-text-color-secondary)]">通过 Sub2API 等中转接入；API key 加密存储，页面只显示末 4 位</span>
      </div>
      <el-button type="primary" @click="openCreate">
        新增配置
      </el-button>
    </div>

    <el-table :data="providers" border>
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
          <el-button link type="primary" :loading="testing === row.id" @click="test(row as Provider)">
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

    <el-dialog v-model="dialogVisible" :title="editingId === null ? '新增模型配置' : '编辑模型配置'" width="560px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="名称" required>
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
        <el-form-item label="Base URL" required>
          <el-input v-model="form.base_url" placeholder="https://your-sub2api.example.com（带不带 /v1 都可以）" />
        </el-form-item>
        <el-form-item label="API Key" :required="editingId === null">
          <el-input v-model="form.api_key" type="password" show-password autocomplete="new-password" :placeholder="editingId === null ? '' : '留空表示不修改'" />
        </el-form-item>
        <el-form-item label="模型" required>
          <el-input v-model="form.model" placeholder="如 claude-sonnet-4-5" />
        </el-form-item>
        <el-form-item label="最大输出">
          <el-input-number v-model="form.max_tokens" :min="256" :max="128000" :step="1024" controls-position="right" />
          <span class="ml-2 text-xs">token</span>
        </el-form-item>
        <el-form-item label="上下文上限">
          <el-input-number v-model="form.context_window" :min="4096" :max="2000000" :step="10000" controls-position="right" />
          <span class="ml-2 text-xs">token，超过时拒绝发送</span>
        </el-form-item>
        <el-form-item label="Temperature">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" :value-on-clear="null" controls-position="right" placeholder="默认" />
          <span class="ml-2 text-xs">留空使用模型默认值</span>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          取消
        </el-button>
        <el-button type="primary" :loading="saving" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
