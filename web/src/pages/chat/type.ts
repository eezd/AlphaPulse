export interface ToolCard {
  id: string
  name: string
  arguments: Record<string, unknown>
  /** 未返回时为 null */
  result: string | null
}

export interface DisplayItem {
  key: string
  kind: "user" | "assistant"
  text: string
  toolCalls: ToolCard[]
  stopped?: boolean
  error?: string
  /** 生成中：这次模型调用已结束，后续文字另起一条 */
  closed?: boolean
  /** 生成中：模型正在思考，还没有输出文字 */
  thinking?: boolean
  /** 已落库的消息时间；生成中的没有 */
  time?: string
}
