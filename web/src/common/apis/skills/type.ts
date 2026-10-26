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

export type ListSkillTreeResponseData = SkillNode[]

export type CreateSkillNodeResponseData = SkillNode

export interface CreateSkillNodeBodyRequestData { parent_id: number | null, kind: NodeKind, name: string, priority?: number, content?: string }

export type PatchSkillNodeResponseData = SkillNode

export interface PatchSkillNodeBodyRequestData { name?: string, priority?: number, enabled?: boolean }

export type MoveSkillNodeResponseData = null

export type DeleteSkillNodeResponseData = null

export type ListSkillVersionsResponseData = SkillVersion[]

export type PublishSkillVersionResponseData = SkillNode

export type SaveSkillDraftResponseData = SkillVersion
