import type { ChatMessage } from "@@/apis/chat/type"
import type { DisplayItem } from "./type"

/** 把会话消息转成展示项：工具结果挂到对应的工具调用上 */
export function toItems(list: ChatMessage[]): DisplayItem[] {
  const items: DisplayItem[] = []
  const calls = new Map<string, DisplayItem["toolCalls"][number]>()
  for (const m of list) {
    if (m.role === "user") {
      items.push({ key: `m${m.id}`, kind: "user", text: m.content.text, toolCalls: [], time: m.created_at })
    } else if (m.role === "assistant") {
      const toolCalls = (m.content.tool_calls ?? []).map(c => ({ ...c, result: null }))
      toolCalls.forEach(c => calls.set(c.id, c))
      items.push({ key: `m${m.id}`, kind: "assistant", text: m.content.text, toolCalls, stopped: m.content.stopped, error: m.content.error, time: m.created_at })
    } else {
      const call = calls.get(m.content.tool_call_id)
      if (call)
        call.result = m.content.result
    }
  }
  return items
}
