<script setup lang="ts">
import ExchangeSettings from "./components/ExchangeSettings.vue"
import ProviderSettings from "./components/ProviderSettings.vue"
</script>

<template>
  <div class="flex flex-col gap-4">
    <ProviderSettings />
    <ExchangeSettings />
  </div>
</template>
