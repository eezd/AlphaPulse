import { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import App from "@/App.vue"
import { logRequestError } from "@/http/axios"
import { TOKEN_KEY, useUserStore } from "@/pinia/stores/user"
import { router } from "@/router"
import "element-plus/dist/index.css"
import "element-plus/theme-chalk/dark/css-vars.css"
import "@fontsource-variable/inter"
import "@fontsource-variable/jetbrains-mono"
import "@@/assets/styles/index.css"
import "virtual:uno.css"

const app = createApp(App).use(createPinia())
const user = useUserStore()
watch(() => user.sessionVersion, () => ElMessageBox.close(), { flush: "sync" })
app.config.errorHandler = logRequestError
window.addEventListener("storage", (event) => {
  if (event.storageArea === localStorage && (event.key === TOKEN_KEY || event.key === null)) {
    user.setToken(localStorage.getItem(TOKEN_KEY) ?? "", false)
    void router.replace({ name: "login", query: { redirect: router.currentRoute.value.fullPath } })
  }
})
app.use(router).mount("#app")
