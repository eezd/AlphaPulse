import { resolve } from "node:path"
import vue from "@vitejs/plugin-vue"
import UnoCSS from "unocss/vite"
import AutoImport from "unplugin-auto-import/vite"
import { ElementPlusResolver } from "unplugin-vue-components/resolvers"
import Components from "unplugin-vue-components/vite"
import { defineConfig } from "vite"

export default defineConfig({
  resolve: {
    alias: {
      "@": resolve(import.meta.dirname, "src")
    }
  },
  server: {
    // 只监听本机，通过 SSH 隧道访问；对外只走 https://alpha.134333.xyz
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        ws: true
      }
    }
  },
  build: {
    reportCompressedSize: false,
    chunkSizeWarningLimit: 2048
  },
  plugins: [
    vue(),
    UnoCSS(),
    AutoImport({
      imports: ["vue", "vue-router", "pinia"],
      dts: "types/auto-imports.d.ts",
      resolvers: [ElementPlusResolver()]
    }),
    Components({
      dts: "types/components.d.ts",
      resolvers: [ElementPlusResolver()]
    })
  ]
})
