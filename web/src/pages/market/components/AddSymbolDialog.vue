<script lang="ts" setup>
import type { Market, TradableSymbol } from "@@/apis/market/type"
import { addSymbolApi, listTradableApi } from "@@/apis/market"

const emit = defineEmits<{ added: [market: Market, symbol: string] }>()
const visible = defineModel<boolean>({ required: true })

const market = ref<Market>("spot")
const symbol = ref("")
const tradable = ref<TradableSymbol[]>([])
const loading = ref(false)
const submitting = ref(false)

const options = computed(() => tradable.value.map(s => ({ value: s.symbol, label: `${s.base}/${s.quote}` })))

watch([visible, market], async ([open]) => {
  if (!open)
    return
  symbol.value = ""
  loading.value = true
  try {
    tradable.value = await listTradableApi(market.value)
  } finally {
    loading.value = false
  }
}, { immediate: true })

async function submit() {
  submitting.value = true
  try {
    await addSymbolApi(market.value, symbol.value)
    ElMessage.success(`已添加 ${symbol.value}，后台开始补齐全历史数据`)
    emit("added", market.value, symbol.value)
    visible.value = false
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="新增交易对" width="460px">
    <el-form label-width="64px">
      <el-form-item label="市场">
        <el-radio-group v-model="market">
          <el-radio-button value="spot">
            现货
          </el-radio-button>
          <el-radio-button value="um">
            U 本位永续
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="交易对">
        <el-select-v2
          v-model="symbol" :options="options" :loading="loading" filterable placeholder="搜索，如 SNDKUSDT"
          class="w-full"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">
        取消
      </el-button>
      <el-button type="primary" :disabled="!symbol" :loading="submitting" @click="submit">
        确认并补齐数据
      </el-button>
    </template>
  </el-dialog>
</template>
