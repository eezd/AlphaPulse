import { createPinia } from "pinia"
import App from "@/App.vue"
import { router } from "@/router"
import "element-plus/dist/index.css"
import "element-plus/theme-chalk/dark/css-vars.css"
import "@/styles/index.css"
import "virtual:uno.css"

createApp(App).use(createPinia()).use(router).mount("#app")
