"""Authenticated API for the two-stage research workspace."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select, update, func, or_, text
from sqlalchemy.dialects.postgresql import insert
from alphapulse.core.db import get_session
from sqlalchemy.ext.asyncio import AsyncSession
from alphapulse.core.auth import require_admin
from alphapulse.api.response import success
from alphapulse.models import NewsItem
from alphapulse.news_research.models import (
    ResearchSettings,
    Package,
    Attempt,
    Event,
    Membership,
    Version,
    Reference,
)
from alphapulse.news_research import service, vocabulary as vocab
from alphapulse.news_research.models import VocabularyVersion
from alphapulse.news_research.contracts import (
    INDEX_SKILL,
    ASSIGN_SKILL,
    RESEARCH_SKILL,
    Strict,
    IndexResult,
    Assignment,
    ResearchResult,
)

router = APIRouter(
    prefix="/api/risk", tags=["news-research"], dependencies=[Depends(require_admin)]
)
DB = Annotated[AsyncSession, Depends(get_session)]


@router.get("/tag-vocabulary")
async def vocabulary(session: DB):
    return success(await vocab.snapshot(session))


@router.put("/tag-vocabulary")
async def save_vocabulary(body: vocab.VocabularyInput, session: DB):
    await session.execute(text("SELECT pg_advisory_xact_lock(20261103, 3)"))
    current = await vocab.snapshot(session)
    if current["revision"] != body.base_revision:
        raise HTTPException(409, "词库已更新，请重新读取后保存")
    row = VocabularyVersion(revision=current["revision"] + 1, entries=[e.model_dump() for e in body.entries])
    session.add(row)
    await session.commit()
    return success({"revision": row.revision, "entries": row.entries})


async def version_references(session, version_id):
    result = []
    for ref in await session.scalars(select(Reference).where(Reference.version_id == version_id)):
        target = await session.get(Version, ref.target_version_id)
        current = await session.get(Event, target.event_id)
        result.append({"event_id": target.event_id, "title": target.output["title"], "version_id": target.id,
                       "role": ref.role, "reason": ref.reason,
                       "changed": target.revision != current.revision or current.state != "done"})
    return result


@router.get("/events/{event_id}/versions/{version_id}")
async def event_version(event_id: int, version_id: int, session: DB):
    row = await session.get(Version, version_id)
    if row is None or row.event_id != event_id or row.state != "done":
        raise HTTPException(404, "事件成功版本不存在")
    return success({"id": row.id, "event_id": row.event_id, "revision": row.revision,
                    "state": row.state, "created_at": row.created_at, "result": row.output,
                    "packages": row.input["packages"], "articles": row.input["articles"],
                    "evidence": row.evidence, "usage": row.usage,
                    "references": await version_references(session, row.id)})


class ModelConfig(Strict):
    provider_id: int = Field(gt=0)
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    skill_id: int = Field(gt=0)


class SettingsInput(Strict):
    index: ModelConfig
    research: ModelConfig
    concurrency: int = Field(default=3, ge=1, le=16)


async def settings_row(session):
    row = await session.get(ResearchSettings, 1)
    if row is None:
        await session.execute(
            insert(ResearchSettings)
            .values(
                id=1,
                config={},
                enabled=False,
                start_at=datetime(2026, 10, 31, 16, tzinfo=UTC),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.commit()
        row = await session.get(ResearchSettings, 1)
    return row


@router.get("/settings")
async def settings(session: DB):
    row = await settings_row(session)
    return success(
        {"config": row.config, "enabled": row.enabled, "start_at": row.start_at}
    )


@router.put("/settings")
async def save(body: SettingsInput, session: DB):
    await settings_row(session)
    await session.execute(
        update(ResearchSettings)
        .where(ResearchSettings.id == 1)
        .values(
            config=body.model_dump(),
            enabled=False,
            control_revision=ResearchSettings.control_revision + 1,
        )
    )
    await session.commit()
    return success(None)


@router.post("/pause")
async def pause(session: DB):
    await settings_row(session)
    await session.execute(
        update(ResearchSettings)
        .where(ResearchSettings.id == 1)
        .values(enabled=False, control_revision=ResearchSettings.control_revision + 1)
    )
    await session.commit()
    return success(None)


@router.post("/enable")
async def enable(session: DB):
    row = await settings_row(session)
    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(20261103, 2)")):
        raise HTTPException(409, "模型验证已在进行中")
    if row.enabled:
        raise HTTPException(409, "请先暂停两阶段处理")
    running = await session.scalar(
        select(func.count())
        .select_from(Package)
        .where(Package.state.in_(["index_processing", "assign_processing"]))
    )
    running += await session.scalar(
        select(func.count()).select_from(Event).where(Event.state == "processing")
    )
    if running:
        raise HTTPException(409, "请等待已接收任务完成后再验证")
    from alphapulse.news_risk.service import complete
    from alphapulse.news_risk.pipeline import validate_event

    fixture = {
        "id": 1,
        "title": "虚构示例：公司维护通知",
        "summary": "",
        "content": "本条为虚构测试。示例公司宣布计划维护，具体时间未知。",
    }
    original_config = row.config
    control_revision = row.control_revision
    try:
        provider, skill, _ = await service.model_config(
            session, original_config, "index"
        )
        indexed, _ = await complete(
            provider,
            skill + "\n" + INDEX_SKILL,
            {"task": "index", "article": fixture},
            IndexResult,
        )
        service.validate_keywords(indexed, fixture)
        assigned, _ = await complete(
            provider,
            skill + "\n" + ASSIGN_SKILL,
            {
                "task": "assign",
                "article": fixture,
                "keywords": indexed.model_dump()["keywords"],
                "events": [],
            },
            Assignment,
        )
        service.validate_assignment(assigned, fixture, set(), indexed.model_dump()["keywords"])
        provider, skill, _ = await service.model_config(
            session, original_config, "research"
        )
        researched, _ = await complete(
            provider,
            skill + "\n" + RESEARCH_SKILL,
            {
                "id": 1,
                "event": {"id": 1, "title": fixture["title"]},
                "articles": [fixture],
                "packages": [{
                    "id": 1, "news_id": 1,
                    "scope_quote": assigned.targets[0].quote,
                    "reason": assigned.targets[0].reason,
                    "keywords": [k for k in indexed.model_dump()["keywords"]
                                 if k["canonical"] in assigned.targets[0].keyword_canonicals],
                    "keyword_canonicals": assigned.targets[0].keyword_canonicals,
                    "scope_selection": "assignment",
                    "other_scopes": [
                        {"title": t.title, "scope_quote": t.quote, "reason": t.reason}
                        for t in assigned.targets[1:]
                    ],
                }],
                "package_ids": [1],
                "previous": None,
            },
            ResearchResult,
        )
        validate_event(researched, {1: fixture})
        service.validate_timeline(researched, {1: 1})
        if (
            researched.id != 1
            or researched.references
            or any(set(p.package_ids) != {1} for p in researched.timeline_points)
        ):
            raise ValueError("测试返回未知对象")
    except Exception as error:
        raise HTTPException(502, f"两阶段验证失败：{error}") from error
    row = await session.get(
        ResearchSettings, 1, populate_existing=True, with_for_update=True
    )
    if row.control_revision != control_revision or row.config != original_config:
        raise HTTPException(409, "验证期间已暂停或设置变化，请重新验证")
    row.enabled = True
    row.control_revision += 1
    await session.commit()
    return success(None)


@router.post("/skill-templates")
async def templates(session: DB):
    from alphapulse.ai.skills import create_node, add_version
    from alphapulse.models import SkillNode, SkillVersion

    ids = {}
    for role, name, content in [
        ("index", "新闻关键词索引与事件归属", INDEX_SKILL + "\n" + ASSIGN_SKILL),
        ("research", "新闻事件整体研究", RESEARCH_SKILL),
    ]:
        node = await session.scalar(
            select(SkillNode)
            .where(SkillNode.parent_id.is_(None), SkillNode.name == name)
            .with_for_update()
        )
        if node is None:
            node = await create_node(session, None, "skill", name, scope="news")
        if node.scope != "news" or node.kind != "skill":
            raise HTTPException(409, "默认Skill名称已被其他用途使用")
        latest = await session.scalar(
            select(SkillVersion)
            .where(SkillVersion.skill_id == node.id)
            .order_by(SkillVersion.version_no.desc())
            .limit(1)
        )
        if latest is None or latest.content != content:
            await add_version(session, node, content, "user", "两阶段新闻研究草稿")
        ids[role] = node.id
    await session.commit()
    return success(ids)


class EnrollInput(Strict):
    news_ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/packages")
async def enroll(body: EnrollInput, session: DB):
    ids = set(body.news_ids)
    if await session.scalar(
        select(func.count()).select_from(NewsItem).where(NewsItem.id.in_(ids))
    ) != len(ids):
        raise HTTPException(404, "新闻不存在")
    await service.enroll(session, ids)
    return success(None)


async def package_out(session, package, news):
    current = package.content_hash == news.content_hash
    memberships = list(
        await session.execute(
            select(Membership, Event)
            .join(Event, Event.id == Membership.event_id)
            .where(Membership.package_id == package.id)
        )
    )
    return {
        "id": package.id,
        "news_id": news.id,
        "title": news.title,
        "url": news.url,
        "published_at": news.published_at,
        "state": package.state if current else "index_pending",
        "error": package.error if current else None,
        "keywords": package.keywords if current else [],
        "content_hash": package.content_hash,
        "events": [
            {"id": e.id, "title": e.title, "reason": m.reason, "quote": m.evidence}
            for m, e in memberships
            if current
        ],
    }


@router.get("/packages")
async def packages(session: DB, q: str = "", page: int = Query(1, ge=1)):
    query = select(Package, NewsItem).join(NewsItem, NewsItem.id == Package.news_id)
    if q:
        condition, _ = service.package_matching([q], await vocab.snapshot(session))
        query = query.where(condition)
    rows = list(
        await session.execute(
            query.order_by(Package.id.desc()).offset((page - 1) * 20).limit(20)
        )
    )
    return success(
        {
            "items": [await package_out(session, p, n) for p, n in rows],
            "total": await session.scalar(
                select(func.count()).select_from(query.subquery())
            ),
        }
    )


@router.get("/news/{news_id}/packages")
async def news_package(
    news_id: int, session: DB, revision_no: int | None = Query(None, ge=1)
):
    news = await session.get(NewsItem, news_id)
    if news is None:
        raise HTTPException(404, "新闻不存在")
    if revision_no is not None and revision_no != news.revision_no:
        return success([])
    package = await session.scalar(select(Package).where(Package.news_id == news_id))
    return success(
        []
        if package is None or package.content_hash != news.content_hash
        else [await package_out(session, package, news)]
    )


@router.get("/packages/{identifier}")
async def package(identifier: int, session: DB):
    row = await session.get(Package, identifier)
    if row is None:
        raise HTTPException(404, "新闻包不存在")
    news = await session.get(NewsItem, row.news_id)
    attempts = list(
        await session.scalars(
            select(Attempt)
            .where(Attempt.package_id == identifier)
            .order_by(Attempt.id.desc())
        )
    )
    return success(
        {
            **await package_out(session, row, news),
            "article": service.article(news),
            "attempts": [
                {
                    "id": a.id,
                    "stage": a.stage,
                    "state": a.state,
                    "error": a.error,
                    "output": a.output,
                    "usage": a.usage,
                }
                for a in attempts
            ],
        }
    )


@router.post("/packages/{identifier}/retry")
async def retry_package(identifier: int, session: DB):
    row = await session.get(Package, identifier, with_for_update=True)
    if row is None:
        raise HTTPException(404, "新闻包不存在")
    if row.state not in ("index_failed", "assign_failed", "done"):
        raise HTTPException(409, "新闻包正在处理")
    row.state = "index_pending" if not row.keywords else "assign_pending"
    row.error = None
    await session.commit()
    return success(None)


@router.get("/events")
async def events(session: DB, q: str = "", page: int = Query(1, ge=1)):
    query = select(Event).where(Event.state != "retired")
    if q:
        query = query.where(
            or_(*(Event.title.icontains(t, autoescape=True) | Event.tags.contains([t])
                  for t in vocab.expand([q], await vocab.snapshot(session))))
        )
    rows = list(
        await session.scalars(
            query.order_by(Event.updated_at.desc(), Event.id)
            .offset((page - 1) * 20)
            .limit(20)
        )
    )
    return success(
        {
            "items": [
                {
                    "id": e.id,
                    "title": e.title,
                    "state": e.state,
                    "revision": e.revision,
                    "error": e.error,
                    "tags": e.tags,
                    "result": e.result,
                    "updated_at": e.updated_at,
                }
                for e in rows
            ],
            "total": await session.scalar(
                select(func.count()).select_from(query.subquery())
            ),
        }
    )


@router.get("/events/{identifier}")
async def event(identifier: int, session: DB):
    row = await session.get(Event, identifier)
    if row is None:
        raise HTTPException(404, "事件不存在")
    members = list(
        await session.execute(
            select(Package, NewsItem)
            .join(NewsItem, NewsItem.id == Package.news_id)
            .join(Membership, Membership.package_id == Package.id)
            .where(Membership.event_id == identifier)
        )
    )
    versions = list(
        await session.scalars(
            select(Version)
            .where(Version.event_id == identifier)
            .order_by(Version.id.desc())
        )
    )
    latest = next((v for v in versions if v.state == "done"), None)
    references = await version_references(session, latest.id) if latest else []
    return success(
        {
            "id": row.id,
            "title": row.title,
            "state": row.state,
            "result": row.result,
            "error": row.error,
            "revision": row.revision,
            "packages": [await package_out(session, p, n) for p, n in members],
            "references": references,
            "versions": [
                {
                    "id": v.id,
                    "revision": v.revision,
                    "state": v.state,
                    "result": v.output,
                    "error": v.error,
                    "usage": v.usage,
                    "evidence": v.evidence,
                    "created_at": v.created_at,
                }
                for v in versions
            ],
        }
    )


@router.post("/events/{identifier}/retry")
async def retry_event(identifier: int, session: DB):
    row = await session.get(Event, identifier, with_for_update=True)
    if row is None:
        raise HTTPException(404, "事件不存在")
    if row.state == "processing":
        raise HTTPException(409, "事件正在处理")
    if row.state == "done":
        service.dirty(row, True)
    else:
        row.state = "pending"
        row.due_at = datetime.now(UTC)
        row.updated_at = row.due_at
        row.error = None
    await session.commit()
    return success(None)
