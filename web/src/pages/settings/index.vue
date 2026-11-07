<script setup lang="ts">
import ExchangeSettings from "./components/ExchangeSettings.vue"
import Jin10Settings from "./components/Jin10Settings.vue"
import ProviderSettings from "./components/ProviderSettings.vue"
</script>

<template>
  <div class="settings-workspace">
    <div class="settings-heading">
      <h1>环境变量配置</h1><p>模型接入、数据连接与模拟盘凭据 · 密钥加密存储，仅显示脱敏信息</p>
    </div><div class="settings-sections">
      <ProviderSettings />
      <Jin10Settings />
      <ExchangeSettings />
    </div>
  </div>
</template>

<style scoped>
.settings-heading {
  padding: 0 0 16px;
}
.settings-heading p {
  font-size: 12px;
  color: #64748b;
}
.settings-sections {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.settings-sections > :first-child {
  grid-column: 1/-1;
}
@media (max-width: 1000px) {
  .settings-sections {
    grid-template-columns: 1fr;
  }
}
</style>
