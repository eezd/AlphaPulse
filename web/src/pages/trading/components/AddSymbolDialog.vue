<script lang="ts" setup>
import type { SymbolOption } from "@@/apis/trading/type"
import { addSymbolApi, listSymbolOptionsApi } from "@@/apis/trading"

const emit = defineEmits<{ added: [symbol: string] }>()
const visible = defineModel<boolean>({ required: true })

const symbol = ref("")
const symbolOptions = ref<SymbolOption[]>([])
const loading = ref(false)
const submitting = ref(false)

const options = computed(() => symbolOptions.value.map(s => ({ value: s.symbol, label: `${s.base}/USDT 永续` })))

watch(visible, async (open) => {
  if (!open)
    return
  symbol.value = ""
  loading.value = true
  try {
    symbolOptions.value = await listSymbolOptionsApi()
  } finally {
    loading.value = false
  }
}, { immediate: true })

async function submit() {
  submitting.value = true
  try {
    await addSymbolApi(symbol.value)
    ElMessage.success(`已加入 ${symbol.value}`)
    emit("added", symbol.value)
    visible.value = false
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="新增交易币种" width="480px">
    <el-form label-width="64px">
      <el-form-item label="币种">
        <el-select-v2
          v-model="symbol" :options="options" :loading="loading" filterable placeholder="搜索，如 BTCUSDT"
          class="w-full"
        />
        <div class="text-xs text-[var(--el-text-color-secondary)]">
          只列出模拟盘和实盘都在交易的加密货币 USDT 永续合约
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">
        取消
      </el-button>
      <el-button type="primary" :disabled="!symbol" :loading="submitting" @click="submit">
        加入
      </el-button>
    </template>
  </el-dialog>
</template>
