<script setup lang="ts">
import type { Exchange, ExchangeName } from "@@/apis/settings/type"
import type { FormInstance, FormRules } from "element-plus"
import { deleteExchangeApi, getExchangeApi, putExchangeApi, testExchangeApi } from "@@/apis/settings"
import { useSessionScope } from "@@/composables/useSessionScope"
import { formatTime } from "@@/utils/text"

const sessionScope = useSessionScope()
// ---------- 模拟盘 ----------

const exchange = ref<Exchange>()
const exchangeDialogVisible = ref(false)
const exchangeForm = ref({ api_key: "", api_secret: "" })
const exchangeSaving = ref(false)
const exchangeTesting = ref(false)
const exchangeFormRef = ref<FormInstance>()
const rules: FormRules = {
  api_key: [{ required: true, whitespace: true, message: "请输入 API Key", trigger: "blur" }],
  api_secret: [{ required: true, whitespace: true, message: "请输入 Secret Key", trigger: "blur" }]
}
const failed = ref(false)
const loading = ref(false)

async function loadExchange() {
  loading.value = true
  try {
    exchange.value = await getExchangeApi()
    failed.value = false
  } catch (error) {
    failed.value = true
    throw error
  } finally {
    loading.value = false
  }
}

async function changeExchange(value: ExchangeName) {
  exchangeSaving.value = true
  try {
    exchange.value = await putExchangeApi({ exchange: value })
    ElMessage.success("已切换模拟盘，另一家交易所的凭证和历史记录仍保留")
  } finally {
    exchangeSaving.value = false
  }
}

function openExchangeEdit() {
  exchangeForm.value = { api_key: "", api_secret: "" }
  exchangeDialogVisible.value = true
}

async function saveExchange() {
  if (exchangeSaving.value) return
  exchangeSaving.value = true
  try {
    await exchangeFormRef.value!.validate()
    exchange.value = await putExchangeApi({ exchange: exchange.value!.exchange, ...exchangeForm.value })
    exchangeDialogVisible.value = false
    ElMessage.success("已保存")
  } finally {
    exchangeSaving.value = false
  }
}

async function removeExchange() {
  await ElMessageBox.confirm("删除模拟盘 API key？", "删除确认", { type: "warning" })
  sessionScope.assertCurrent()
  exchange.value = await deleteExchangeApi()
  ElMessage.success("已删除")
}

async function runExchangeTest() {
  exchangeTesting.value = true
  try {
    const r = await testExchangeApi()
    if (r.ok) {
      const mode = r.dual_side_position ? "双向持仓（开始交易时会切换为单向）" : "单向持仓"
      ElMessage.success({ message: `连接成功，用时 ${r.elapsed_ms}ms；可用 ${r.available_usdt} USDT，钱包余额 ${r.wallet_usdt} USDT；${mode}`, duration: 8000 })
    } else {
      ElMessage.error({ message: `连接失败（${r.elapsed_ms}ms）：${r.error}`, duration: 8000 })
    }
  } finally {
    exchangeTesting.value = false
  }
}

onMounted(loadExchange)
</script>

<template>
  <section class="min-w-0">
    <el-alert v-if="failed" type="error" :closable="false" title="交易所配置读取失败">
      <el-button link @click="loadExchange">
        重试
      </el-button>
    </el-alert>
    <el-card v-loading="loading" shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <div>
            模拟盘交易所
            <el-select :model-value="exchange?.exchange" class="ml-3 w-36" :disabled="!exchange || exchange.running || exchangeSaving || exchangeTesting" @update:model-value="changeExchange">
              <el-option value="binance" label="币安模拟盘" />
              <el-option value="bybit" label="Bybit 模拟盘" />
            </el-select>
            <div class="mt-2 text-xs text-slate-500 font-normal">
              {{ exchange?.exchange === "bybit" ? "在 Bybit 主站切换到模拟交易后创建 API Key，并开启合约交易权限；实盘和 Testnet 的 Key 不可用。" : "在 demo.binance.com 的 API 管理创建 Key，并开启合约交易权限。" }}
              两家凭证分别保存；切换不会撤销原交易所的挂单，旧挂单仍须在原平台处理。
            </div>
          </div>
          <div>
            <el-button :disabled="!exchange?.configured" :loading="exchangeTesting" @click="runExchangeTest">
              测试连接
            </el-button>
            <el-button type="primary" :disabled="!exchange || exchange.running || exchangeSaving || exchangeTesting" @click="openExchangeEdit">
              {{ exchange?.configured ? "修改 key" : "填写 key" }}
            </el-button>
            <el-button v-if="exchange?.configured" type="danger" plain :disabled="exchange.running" @click="removeExchange">
              删除
            </el-button>
          </div>
        </div>
      </template>
      <el-descriptions v-if="exchange" :column="3" border>
        <el-descriptions-item label="API Key">
          {{ exchange.configured ? `****${exchange.api_key_hint}` : "未配置" }}
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag v-if="exchange.running" type="success" size="small">
            有币种在交易（全部停止后才能修改 key）
          </el-tag>
          <span v-else>未运行</span>
        </el-descriptions-item>
        <el-descriptions-item label="更新时间">
          {{ exchange.updated_at ? formatTime(exchange.updated_at, "MM-DD HH:mm") : "-" }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-dialog v-model="exchangeDialogVisible" :title="`${exchange?.exchange === 'bybit' ? 'Bybit' : '币安'}模拟盘 API Key`" width="min(560px, 94vw)" :close-on-click-modal="!exchangeSaving" :close-on-press-escape="!exchangeSaving" :show-close="!exchangeSaving">
      <el-form ref="exchangeFormRef" :model="exchangeForm" :rules="rules" :disabled="exchangeSaving" label-width="100px">
        <el-form-item prop="api_key" label="API Key" required>
          <el-input v-model="exchangeForm.api_key" type="password" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item prop="api_secret" label="Secret Key" required>
          <el-input v-model="exchangeForm.api_secret" type="password" show-password autocomplete="new-password" />
          <div class="text-xs text-[var(--el-text-color-secondary)]">
            两项都要填；加密保存，之后不再显示
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="exchangeSaving" @click="exchangeDialogVisible = false">
          取消
        </el-button>
        <el-button type="primary" :loading="exchangeSaving" :disabled="!exchangeForm.api_key.trim() || !exchangeForm.api_secret.trim()" @click="saveExchange">
          保存
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
