"""Skills 管理：树、节点、版本、发布与回退"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.ai.skills import SkillError, add_version, create_node, load_tree
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.models import SkillNode, SkillVersion

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(require_admin)])


class NodeOut(BaseModel):
    id: int
    parent_id: int | None
    kind: Literal["folder", "skill"]
    name: str
    path: str
    sort_order: int
    priority: int
    enabled: bool
    published_version_no: int | None
    latest_version_no: int | None
    has_draft: bool
    updated_at: datetime


class NodeIn(BaseModel):
    parent_id: int | None = None
    kind: Literal["folder", "skill"]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    priority: int = 0
    # 新建 skill 时的初始内容，保存为 v1 草稿（未发布）；为空则不建版本
    content: str | None = None


class NodePatch(BaseModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    priority: int | None = None
    enabled: bool | None = None


class MoveIn(BaseModel):
    # null 表示移到根目录
    parent_id: int | None
    # 在新父节点下的位置（从 0 开始）
    index: Annotated[int, Field(ge=0)]


class VersionOut(BaseModel):
    id: int
    version_no: int
    content: str
    source: Literal["user", "chat"]
    note: str
    created_at: datetime


class VersionIn(BaseModel):
    content: str
    note: str = ""


class PublishIn(BaseModel):
    version_id: int


async def get_node(session: AsyncSession, node_id: int, kind: str | None = None) -> SkillNode:
    node = await session.get(SkillNode, node_id)
    if node is None or (kind and node.kind != kind):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "节点不存在")
    return node


async def node_out(session: AsyncSession, node_id: int) -> NodeOut:
    info = next(i for i in await load_tree(session) if i.node.id == node_id)
    return NodeOut(**{**info.node.__dict__, "path": info.path, "published_version_no": info.published_version_no,
                      "latest_version_no": info.latest_version_no, "has_draft": info.has_draft})


def bad_request(e: SkillError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("/tree")
async def tree(session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[NodeOut]]:
    """扁平列表，前端按 parent_id 组装成树"""
    return success([
        NodeOut(**{**i.node.__dict__, "path": i.path, "published_version_no": i.published_version_no,
                   "latest_version_no": i.latest_version_no, "has_draft": i.has_draft})
        for i in await load_tree(session)
    ])


@router.post("/nodes", status_code=status.HTTP_201_CREATED)
async def create(body: NodeIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[NodeOut]:
    try:
        node = await create_node(session, body.parent_id, body.kind, body.name, body.priority)
        # 没有初始内容时不建空版本，第一次保存即 v1
        if body.kind == "skill" and body.content:
            await add_version(session, node, body.content, "user", "新建")
    except SkillError as e:
        raise bad_request(e) from None
    await session.commit()
    return success(await node_out(session, node.id))


@router.patch("/nodes/{node_id}")
async def patch(node_id: int, body: NodePatch, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[NodeOut]:
    node = await get_node(session, node_id)
    if body.name is not None:
        name = body.name.strip()
        if not name or "/" in name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "名称不能为空，也不能包含 /")
        duplicate = await session.scalar(select(SkillNode.id).where(
            SkillNode.parent_id.is_not_distinct_from(node.parent_id), SkillNode.name == name, SkillNode.id != node_id
        ))
        if duplicate:
            raise HTTPException(status.HTTP_409_CONFLICT, f"同一文件夹下已存在：{name}")
        node.name = name
    if body.priority is not None:
        node.priority = body.priority
    if body.enabled is not None:
        node.enabled = body.enabled
    await session.commit()
    return success(await node_out(session, node_id))


@router.post("/nodes/{node_id}/move")
async def move(node_id: int, body: MoveIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[None]:
    node = await get_node(session, node_id)
    if body.parent_id is not None:
        parent = await get_node(session, body.parent_id)
        if parent.kind != "folder":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "只能移动到文件夹中")
        # 不能移到自己或自己的子孙下面
        cursor: SkillNode | None = parent
        while cursor is not None:
            if cursor.id == node_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能移动到自身或其子文件夹中")
            cursor = await session.get(SkillNode, cursor.parent_id) if cursor.parent_id else None
    duplicate = await session.scalar(select(SkillNode.id).where(
        SkillNode.parent_id.is_not_distinct_from(body.parent_id), SkillNode.name == node.name, SkillNode.id != node_id
    ))
    if duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, f"目标文件夹下已存在同名节点：{node.name}")
    siblings = (await session.scalars(
        select(SkillNode).where(SkillNode.parent_id.is_not_distinct_from(body.parent_id), SkillNode.id != node_id)
        .order_by(SkillNode.sort_order, SkillNode.id)
    )).all()
    ordered = list(siblings)
    ordered.insert(min(body.index, len(ordered)), node)
    node.parent_id = body.parent_id
    for i, sibling in enumerate(ordered):
        sibling.sort_order = i
    await session.commit()
    return success(None)


@router.delete("/nodes/{node_id}")
async def delete(node_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[None]:
    node = await get_node(session, node_id)
    if node.kind == "folder" and await session.scalar(select(func.count()).where(SkillNode.parent_id == node_id)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件夹不为空，请先移走或删除其中的节点")
    # 先解除对版本的引用，版本随节点级联删除
    node.published_version_id = None
    await session.flush()
    await session.delete(node)
    await session.commit()
    return success(None)


@router.get("/{skill_id}/versions")
async def versions(skill_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[VersionOut]]:
    await get_node(session, skill_id, "skill")
    rows = await session.scalars(select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version_no.desc()))
    return success([VersionOut.model_validate(v, from_attributes=True) for v in rows])


@router.post("/{skill_id}/versions", status_code=status.HTTP_201_CREATED)
async def save_draft(skill_id: int, body: VersionIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[VersionOut]:
    skill = await get_node(session, skill_id, "skill")
    version = await add_version(session, skill, body.content, "user", body.note)
    await session.commit()
    await session.refresh(version)
    return success(VersionOut.model_validate(version, from_attributes=True))


@router.post("/{skill_id}/publish")
async def publish(skill_id: int, body: PublishIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[NodeOut]:
    """发布指定版本；发布旧版本即回退"""
    skill = await get_node(session, skill_id, "skill")
    version = await session.get(SkillVersion, body.version_id)
    if version is None or version.skill_id != skill_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "版本不存在")
    skill.published_version_id = version.id
    await session.commit()
    return success(await node_out(session, skill_id))
