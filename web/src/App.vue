<script lang="ts" setup>
import zhCn from "element-plus/es/locale/lang/zh-cn"
import { useUserStore } from "@/pinia/stores/user"

const user = useUserStore()
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view :key="user.sessionVersion" />
  </el-config-provider>
</template>
