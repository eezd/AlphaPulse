import dayjs from "dayjs"

export function formatTime(value: string | null | undefined, pattern = "MM-DD HH:mm:ss") {
  return value ? dayjs(value).format(pattern) : "-"
}

/** 两个时间点之间的耗时，如「1m 52s」 */
export function formatDuration(from: string, to: string) {
  const seconds = Math.round((new Date(to).getTime() - new Date(from).getTime()) / 1000)
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`
}

/** HTML 转纯文本行；DOMParser 解析的文档不会执行脚本 */
export function htmlToLines(html: string): string[] {
  const marked = html.replace(/<br\s*\/?>|<\/(?:p|div|li|h\d)>/gi, "\n")
  const text = new DOMParser().parseFromString(marked, "text/html").body.textContent ?? ""
  return text.split("\n").map(line => line.trim()).filter(Boolean)
}

export interface DiffLine {
  type: "same" | "add" | "del"
  text: string
}

/** 基于最长公共子序列的逐行对比 */
export function diffLines(before: string[], after: string[]): DiffLine[] {
  const n = before.length
  const m = after.length
  const lcs = Array.from({ length: n + 1 }, () => Array.from<number>({ length: m + 1 }).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--)
      lcs[i][j] = before[i] === after[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
  }
  const result: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (before[i] === after[j]) {
      result.push({ type: "same", text: before[i] })
      i++
      j++
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      result.push({ type: "del", text: before[i++] })
    } else {
      result.push({ type: "add", text: after[j++] })
    }
  }
  while (i < n) result.push({ type: "del", text: before[i++] })
  while (j < m) result.push({ type: "add", text: after[j++] })
  return result
}
