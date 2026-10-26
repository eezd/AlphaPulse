import type * as Skills from "./type"
import { request } from "@/http/axios"

/** 读取 Skills 树 */
export function listSkillTreeApi() {
  return request<Skills.ListSkillTreeResponseData>({
    url: "/skills/tree",
    method: "get"
  })
}

/** 新增文件夹或 Skill */
export function createSkillNodeApi(body: Skills.CreateSkillNodeBodyRequestData) {
  return request<Skills.CreateSkillNodeResponseData>({
    url: "/skills/nodes",
    method: "post",
    data: body
  })
}

/** 修改 Skill 元信息 */
export function patchSkillNodeApi(id: number, body: Skills.PatchSkillNodeBodyRequestData) {
  return request<Skills.PatchSkillNodeResponseData>({
    url: `/skills/nodes/${id}`,
    method: "patch",
    data: body
  })
}

/** 移动 Skill 节点 */
export function moveSkillNodeApi(id: number, parentId: number | null, index: number) {
  return request<Skills.MoveSkillNodeResponseData>({
    url: `/skills/nodes/${id}/move`,
    method: "post",
    data: { parent_id: parentId, index }
  })
}

/** 删除 Skill 节点 */
export function deleteSkillNodeApi(id: number) {
  return request<Skills.DeleteSkillNodeResponseData>({
    url: `/skills/nodes/${id}`,
    method: "delete"
  })
}

/** 读取 Skill 历史版本 */
export function listSkillVersionsApi(id: number) {
  return request<Skills.ListSkillVersionsResponseData>({
    url: `/skills/${id}/versions`,
    method: "get"
  })
}

/** 发布指定 Skill 版本 */
export function publishSkillVersionApi(id: number, versionId: number) {
  return request<Skills.PublishSkillVersionResponseData>({
    url: `/skills/${id}/publish`,
    method: "post",
    data: { version_id: versionId }
  })
}

/** 保存 Skill 草稿，不自动发布 */
export function saveSkillDraftApi(id: number, content: string, note = "") {
  return request<Skills.SaveSkillDraftResponseData>({
    url: `/skills/${id}/versions`,
    method: "post",
    data: { content, note }
  })
}
