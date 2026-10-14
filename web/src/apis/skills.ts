import { http } from "@/http"

export type NodeKind = "folder" | "skill"

export interface SkillNode {
  id: number
  parent_id: number | null
  kind: NodeKind
  name: string
  path: string
  sort_order: number
  priority: number
  enabled: boolean
  published_version_no: number | null
  latest_version_no: number | null
  has_draft: boolean
  updated_at: string
}

export interface SkillVersion {
  id: number
  version_no: number
  content: string
  source: "user" | "chat"
  note: string
  created_at: string
}

export function listSkillTree(): Promise<SkillNode[]> {
  return http.get("/skills/tree")
}

export function createSkillNode(body: { parent_id: number | null, kind: NodeKind, name: string, priority?: number, content?: string }): Promise<SkillNode> {
  return http.post("/skills/nodes", body)
}

export function patchSkillNode(id: number, body: { name?: string, priority?: number, enabled?: boolean }): Promise<SkillNode> {
  return http.patch(`/skills/nodes/${id}`, body)
}

export function moveSkillNode(id: number, parentId: number | null, index: number): Promise<void> {
  return http.post(`/skills/nodes/${id}/move`, { parent_id: parentId, index })
}

export function deleteSkillNode(id: number): Promise<void> {
  return http.delete(`/skills/nodes/${id}`)
}

export function listSkillVersions(id: number): Promise<SkillVersion[]> {
  return http.get(`/skills/${id}/versions`)
}

export function saveSkillDraft(id: number, content: string, note = ""): Promise<SkillVersion> {
  return http.post(`/skills/${id}/versions`, { content, note })
}

export function publishSkillVersion(id: number, versionId: number): Promise<SkillNode> {
  return http.post(`/skills/${id}/publish`, { version_id: versionId })
}
