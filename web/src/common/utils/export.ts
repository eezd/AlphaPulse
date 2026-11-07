export function downloadText(name: string, content: string, type = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export function downloadCsv(name: string, rows: unknown[][]) {
  const cell = (value: unknown) => {
    let text = String(value ?? "")
    if (typeof value === "string" && /^\s*[=+@-]/.test(text)) text = `'${text}`
    return `"${text.replaceAll("\"", "\"\"")}"`
  }
  downloadText(name, `\uFEFF${rows.map(row => row.map(cell).join(",")).join("\r\n")}`, "text/csv;charset=utf-8")
}
