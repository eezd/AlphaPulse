export interface StreamEvent {
  event: string
  data: string
}

/** 按 SSE 行语义解析，支持分块、CRLF、多行 data 和最后一个未换行块。 */
export async function* parseEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let event = "message"
  let data: string[] = []
  function line(value: string): StreamEvent | undefined {
    if (!value) {
      const result = data.length ? { event, data: data.join("\n") } : undefined
      event = "message"
      data = []
      return result
    }
    if (value.startsWith(":"))
      return
    const colon = value.indexOf(":")
    const field = colon < 0 ? value : value.slice(0, colon)
    let content = colon < 0 ? "" : value.slice(colon + 1)
    if (content.startsWith(" ")) content = content.slice(1)
    if (field === "event") event = content
    if (field === "data") data.push(content)
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      while (true) {
        const end = buffer.search(/[\r\n]/)
        if (end < 0) break
        if (!done && buffer[end] === "\r" && end === buffer.length - 1)
          break
        const width = buffer[end] === "\r" && buffer[end + 1] === "\n" ? 2 : 1
        const result = line(buffer.slice(0, end))
        buffer = buffer.slice(end + width)
        if (result) yield result
      }
      if (done) {
        if (buffer) line(buffer)
        const result = line("")
        if (result) yield result
        return
      }
    }
  } finally {
    await reader.cancel()
    reader.releaseLock()
  }
}
