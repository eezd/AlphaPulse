<script setup lang="ts">
import { getOpenApiApi } from "@@/apis/docs"
import { rejectAuthentication } from "@/http/axios"
import { useUserStore } from "@/pinia/stores/user"

const target = ref<HTMLElement>()
const error = ref("")
const user = useUserStore()
const version = user.sessionVersion
const controller = new AbortController()
let disposed = false

interface SwaggerWindow extends Window {
  SwaggerUIBundle?: (options: Record<string, unknown>) => unknown
}

onMounted(async () => {
  try {
    const spec = await getOpenApiApi(controller.signal)
    const source = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
    if (!(window as SwaggerWindow).SwaggerUIBundle) {
      await new Promise<void>((resolve, reject) => {
        const script = document.createElement("script")
        script.src = source
        script.onload = () => resolve()
        script.onerror = () => reject(new Error("接口文档资源加载失败"))
        document.head.append(script)
      })
    }
    if (version !== user.sessionVersion || disposed) {
      return
    }
    const render = (window as SwaggerWindow).SwaggerUIBundle!
    render({
      spec,
      domNode: target.value,
      requestInterceptor: (request: { headers: Record<string, string> }) => {
        if (version !== user.sessionVersion) throw new Error("会话已切换")
        request.headers.Authorization = `Bearer ${user.token}`
        return request
      },
      responseInterceptor: (response: { status: number }) => {
        if (response.status === 401) rejectAuthentication(version)
        return response
      }
    })
  } catch (reason) {
    if (!disposed && version === user.sessionVersion)
      error.value = (reason as Error).message
  }
})
onBeforeUnmount(() => {
  disposed = true
  controller.abort()
})
</script>

<template>
  <div class="h-full overflow-auto bg-white p-4">
    <p v-if="error" role="alert">
      {{ error }}
    </p>
    <div ref="target" />
  </div>
</template>

<style>
@import url("https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css");
</style>
