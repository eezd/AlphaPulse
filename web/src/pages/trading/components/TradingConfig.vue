<script setup lang="ts">
import type { Effort, Provider } from "@@/apis/settings/type"
import type { PatchConfigBodyRequestData, TradingStatus } from "@@/apis/trading/type"
import type { FormInstance, FormRules } from "element-plus"
import { patchConfigApi } from "@@/apis/trading"
import { EFFORTS } from "@@/constants/ai"

const { status, providers } = defineProps<{ status: TradingStatus, providers: Provider[] }>()
const emit = defineEmits<{ saved: [status: TradingStatus] }>()
const formRef = ref<FormInstance>()
const saving = ref(false)
const form = reactive({ provider_id: null as number | null, reasoning_effort: null as Effort | null, daily_analysis_limit: 1 })
const baseline = ref({ ...form })
const dirty = computed(() => form.provider_id !== baseline.value.provider_id || form.reasoning_effort !== baseline.value.reasoning_effort || form.daily_analysis_limit !== baseline.value.daily_analysis_limit)
const rules: FormRules = {
  daily_analysis_limit: [{ required: true, type: "integer", min: 1, max: 2147483647, message: "请输入 1–2147483647 之间的整数", trigger: "blur" }]
}

function reset() {
  Object.assign(form, { provider_id: status.provider_id, reasoning_effort: status.reasoning_effort, daily_analysis_limit: status.today.limit })
  baseline.value = { ...form }
  formRef.value?.clearValidate()
}
// 轮询更新账户状态时，保留正在编辑或保存的草稿。
watch(() => status, () => {
  if (!dirty.value && !saving.value) reset()
}, { immediate: true })

async function save() {
  if (saving.value || !dirty.value) return
  saving.value = true
  try {
    await formRef.value!.validate()
    const body: PatchConfigBodyRequestData = {}
    if (form.provider_id !== baseline.value.provider_id) body.provider_id = form.provider_id
    if (form.reasoning_effort !== baseline.value.reasoning_effort) body.reasoning_effort = form.reasoning_effort
    if (form.daily_analysis_limit !== baseline.value.daily_analysis_limit) body.daily_analysis_limit = form.daily_analysis_limit
    const result = await patchConfigApi(body)
    Object.assign(form, { provider_id: result.provider_id, reasoning_effort: result.reasoning_effort, daily_analysis_limit: result.today.limit })
    baseline.value = { ...form }
    emit("saved", result)
    ElMessage.success("交易配置已保存，即时生效")
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-card shadow="never">
    <el-form ref="formRef" :model="form" :rules="rules" :disabled="saving" inline class="trading-config" @submit.prevent="save">
      <el-form-item label="模型">
        <el-select v-model="form.provider_id" class="w-56" clearable :value-on-clear="null" placeholder="跟随默认配置">
          <el-option v-for="provider in providers" :key="provider.id" :value="provider.id" :label="`${provider.name}（${provider.model}）`" />
        </el-select>
      </el-form-item>
      <el-form-item label="思考强度">
        <el-select v-model="form.reasoning_effort" class="w-36" clearable :value-on-clear="null" placeholder="跟随配置">
          <el-option v-for="effort in EFFORTS" :key="effort" :value="effort" :label="effort" />
        </el-select>
      </el-form-item>
      <el-form-item label="每日分析上限" prop="daily_analysis_limit">
        <el-input-number v-model="form.daily_analysis_limit" :min="1" :max="2147483647" :precision="0" controls-position="right" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" native-type="submit" :disabled="!dirty" :loading="saving">
          保存配置
        </el-button>
        <el-button :disabled="!dirty" @click="reset">
          取消修改
        </el-button>
        <span v-if="dirty" class="ml-2 text-xs text-amber-600">未保存</span>
      </el-form-item>
    </el-form>
    <p class="m-0 text-xs text-slate-500">
      所有币种和交易所共用每日额度。手动分析计数但不受上限限制；降低上限不会取消正在进行的分析或持仓管理。
    </p>
  </el-card>
</template>

<style scoped>
.trading-config :deep(.el-form-item) {
  margin-bottom: 12px;
  margin-right: 16px;
}
</style>
