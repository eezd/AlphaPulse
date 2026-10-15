import { defineConfig, presetAttributify, presetWind3 } from "unocss"
import { PALETTE } from "./src/styles/palette"

// camelCase → kebab-case：类名写 bg-ap-panel-high，CSS 变量为 --ap-panel-high
const kebab = (name: string) => name.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)
const apColors = Object.fromEntries(Object.entries(PALETTE).map(([k, v]) => [kebab(k), v]))
const CJK = `"PingFang SC", "Microsoft YaHei", "Noto Sans SC"`

export default defineConfig({
  // 预设
  presets: [
    // 属性化模式 & 无值的属性模式
    presetAttributify({
      prefix: "un-",
      prefixedOnly: false
    }),
    // 默认预设
    presetWind3({
      important: "#app"
    })
  ],
  theme: {
    colors: { ap: apColors },
    fontFamily: {
      // 界面、表格、数字
      mono: `"JetBrains Mono Variable", ui-monospace, ${CJK}, monospace`,
      // 标题
      head: `"Space Grotesk Variable", ${CJK}, sans-serif`,
      // 正文类：新闻、Markdown、Chat 回复
      sans: `-apple-system, BlinkMacSystemFont, "Segoe UI", ${CJK}, sans-serif`
    }
  },
  preflights: [{
    getCSS: ({ theme }) => {
      const fonts = (theme as { fontFamily: Record<string, string> }).fontFamily
      return `:root{${Object.entries(apColors).map(([k, v]) => `--ap-${k}:${v};`).join("")}`
        + `--ap-font-mono:${fonts.mono};--ap-font-head:${fonts.head};--ap-font-sans:${fonts.sans};}`
    }
  }],
  // 自定义规则
  rules: [],
  // 自定义快捷方式
  shortcuts: {
    "wh-full": "w-full h-full",
    "flex-center": "flex justify-center items-center",
    "flex-x-center": "flex justify-center",
    "flex-y-center": "flex items-center",
    // 面板标题条里的标题
    "panel-title": "font-head text-[12px] font-bold tracking-wider text-ap-text"
  }
})
