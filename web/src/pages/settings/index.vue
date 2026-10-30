<script setup lang="ts">
import ExchangeSettings from "./components/ExchangeSettings.vue"
import Jin10Settings from "./components/Jin10Settings.vue"
import ProviderSettings from "./components/ProviderSettings.vue"
</script>

<template>
  <div class="flex flex-col gap-4">
    <ProviderSettings />
    <Jin10Settings />
    <ExchangeSettings />
  </div>
</template>
