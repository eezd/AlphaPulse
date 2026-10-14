"""skills 树与版本的共用逻辑，供 skills 接口和 Chat 工具使用"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.models import SkillNode, SkillVersion


class SkillError(Exception):
    """可直接展示给用户（或模型）的业务错误"""


@dataclass
class NodeInfo:
    node: SkillNode
    path: str
    published_version_no: int | None
    latest_version_no: int | None

    @property
    def has_draft(self) -> bool:
        # 存在比已发布版本更新的版本
        return self.latest_version_no is not None and self.latest_version_no != self.published_version_no


async def load_tree(session: AsyncSession) -> list[NodeInfo]:
    """所有节点及其路径和版本状态，按父节点、sort_order 排序"""
    nodes = (await session.scalars(select(SkillNode).order_by(SkillNode.parent_id.nulls_first(), SkillNode.sort_order, SkillNode.id))).all()
    latest = dict((await session.execute(select(SkillVersion.skill_id, func.max(SkillVersion.version_no)).group_by(SkillVersion.skill_id))).all())
    published = dict((await session.execute(
        select(SkillNode.id, SkillVersion.version_no).join(SkillVersion, SkillVersion.id == SkillNode.published_version_id)
    )).all())
    by_id = {n.id: n for n in nodes}

    def path(node: SkillNode) -> str:
        parts = [node.name]
        while node.parent_id is not None:
            node = by_id[node.parent_id]
            parts.append(node.name)
        return "/".join(reversed(parts))

    return [NodeInfo(n, path(n), published.get(n.id), latest.get(n.id)) for n in nodes]


async def find_by_path(session: AsyncSession, path: str, kind: str | None = None) -> SkillNode:
    path = path.strip("/")
    for info in await load_tree(session):
        if info.path == path and (kind is None or info.node.kind == kind):
            return info.node
    raise SkillError(f"找不到{'文件夹' if kind == 'folder' else 'skill'}：{path}")


async def add_version(session: AsyncSession, skill: SkillNode, content: str, source: str, note: str) -> SkillVersion:
    """追加一个新版本（不发布）；调用方负责 commit"""
    if skill.kind != "skill":
        raise SkillError("文件夹没有内容版本")
    # 锁住 skill 行，避免并发保存得到相同的版本号
    await session.execute(select(SkillNode.id).where(SkillNode.id == skill.id).with_for_update())
    current = await session.scalar(select(func.max(SkillVersion.version_no)).where(SkillVersion.skill_id == skill.id))
    version = SkillVersion(skill_id=skill.id, version_no=(current or 0) + 1, content=content, source=source, note=note)
    session.add(version)
    await session.flush()
    return version


async def create_node(session: AsyncSession, parent_id: int | None, kind: str, name: str, priority: int = 0) -> SkillNode:
    name = name.strip()
    if not name or "/" in name:
        raise SkillError("名称不能为空，也不能包含 /")
    if parent_id is not None:
        parent = await session.get(SkillNode, parent_id)
        if parent is None or parent.kind != "folder":
            raise SkillError("只能在文件夹下新建")
    same_parent = SkillNode.parent_id.is_not_distinct_from(parent_id)
    if await session.scalar(select(SkillNode.id).where(same_parent, SkillNode.name == name)):
        raise SkillError(f"同一文件夹下已存在：{name}")
    last = await session.scalar(select(func.max(SkillNode.sort_order)).where(same_parent))
    node = SkillNode(parent_id=parent_id, kind=kind, name=name, priority=priority, sort_order=(last or 0) + 1, enabled=True)
    session.add(node)
    await session.flush()
    return node


async def published_skills(session: AsyncSession) -> list[tuple[NodeInfo, str]]:
    """进入 prompt 的 skills：已发布、已启用、所有上级文件夹也未停用；按优先级从高到低"""
    tree = await load_tree(session)
    by_id = {i.node.id: i.node for i in tree}

    def active(node: SkillNode) -> bool:
        while True:
            if not node.enabled:
                return False
            if node.parent_id is None:
                return True
            node = by_id[node.parent_id]

    chosen = [i for i in tree if i.node.kind == "skill" and i.node.published_version_id is not None and active(i.node)]
    contents = dict((await session.execute(
        select(SkillVersion.id, SkillVersion.content).where(SkillVersion.id.in_([i.node.published_version_id for i in chosen]))
    )).all())
    chosen.sort(key=lambda i: (-i.node.priority, i.path))
    return [(i, contents[i.node.published_version_id]) for i in chosen]
