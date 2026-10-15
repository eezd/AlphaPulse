/**
 * 全站配色，取自 Stitch 稿 code.html 的 Tailwind token（screen.png 取样核实）。
 * uno.config.ts 用它生成 ap-* 类名和 --ap-* CSS 变量；KLineChart 和新闻 iframe 读不到 CSS 变量，直接引用这里的值。
 */
export const PALETTE = {
  bg: "#0a141f",
  sunken: "#050f19",
  panelLow: "#131c27",
  panel: "#17202b",
  panelHigh: "#212b36",
  panelHighest: "#2c3641",
  line: "#3c4a40",
  faint: "#859489",
  text: "#d9e3f3",
  muted: "#bbcabd",
  brand: "#ffd162",
  up: "#45e89b",
  down: "#ffb4ab",
  warn: "#ff8f1f",
  // K 线柱体需要更饱和的涨跌色
  candleUp: "#0ecb81",
  candleDown: "#f6465d"
} as const
