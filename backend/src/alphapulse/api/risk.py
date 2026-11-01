"""Authenticated news risk research endpoints."""
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Annotated
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from alphapulse.ai.llm import Effort
from alphapulse.api.response import success
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.models import NewsItem, NewsRevision
from alphapulse.models.risk import RiskSettings, RiskSnapshot, ArticleAnalysis, NewsSummary, SummaryArticle, SummaryVersion
from alphapulse.news_risk.service import default_settings, complete

router = APIRouter(prefix='/api/risk', tags=['risk'], dependencies=[Depends(require_admin)])
DB = Annotated[AsyncSession, Depends(get_session)]


class RiskWindow(IntEnum):
    DAY = 24
    TWO_DAYS = 48
    THREE_DAYS = 72
    WEEK = 168


class SettingsIn(BaseModel):
    enabled: bool = False
    provider_id: int | None = Field(default=None, gt=0)
    model: str = Field(default='', max_length=128)
    second_provider_id: int | None = Field(default=None, gt=0)
    second_model: str = Field(default='', max_length=128)
    third_provider_id: int | None = Field(default=None, gt=0)
    third_model: str = Field(default='', max_length=128)
    reasoning_effort: Effort | None = None
    second_reasoning_effort: Effort | None = None
    third_reasoning_effort: Effort | None = None
    grouping_skill_id: int | None = Field(default=None, gt=0)
    analysis_skill_id: int | None = Field(default=None, gt=0)
    outlook_skill_id: int | None = Field(default=None, gt=0)


def config_out(value):
    return {**{key: getattr(value, key) for key in SettingsIn.model_fields}, 'start_at': value.start_at}


@router.get('/settings')
async def settings(session: DB):
    return success(config_out(await default_settings(session)))


@router.put('/settings')
async def save_settings(body: SettingsIn, session: DB):
    previous = await session.get(RiskSettings, 1)
    if body.enabled:
        raise HTTPException(409, '请先使用测试并启用按钮验证三阶段模型和Skills')
    value = RiskSettings(id=1, **body.model_dump(), start_at=previous.start_at if previous else None)
    await session.merge(value); await session.commit()
    return success(None)


class TemplatesIn(BaseModel):
    refresh_existing: bool = False


@router.post('/skill-templates')
async def templates(session: DB, body: Annotated[TemplatesIn | None, Body()] = None):
    from alphapulse.ai.skills import create_node, add_version
    from alphapulse.news_risk.pipeline import TEMPLATES
    from alphapulse.models import SkillNode, SkillVersion
    result = {}
    for role, (name, content) in TEMPLATES.items():
        node = await session.scalar(select(SkillNode).where(SkillNode.parent_id.is_(None), SkillNode.name == name).with_for_update())
        if node is not None and (node.scope != 'news' or node.kind != 'skill'):
            raise HTTPException(409, f'{name}已被其他用途或类型使用，请先重命名')
        if node is None:
            node = await create_node(session, None, 'skill', name, scope='news')
            await add_version(session, node, content, 'user', '新闻AI默认草稿')
        elif body is not None and body.refresh_existing:
            latest = await session.scalar(select(SkillVersion).where(SkillVersion.skill_id == node.id)
                .order_by(SkillVersion.version_no.desc()).limit(1))
            if latest is None or latest.content != content:
                await add_version(session, node, content, 'user', '更新新闻AI默认草稿')
        result[role+'_skill_id' if role != 'analysis' else 'analysis_skill_id'] = node.id
    await session.commit()
    return success(result)


@router.post('/enable')
async def enable(body: SettingsIn, session: DB):
    from alphapulse.news_risk.pipeline import configuration, Grouping, summarize_articles, Evidence, validate_evidence
    from alphapulse.news_risk import briefing
    before = await session.get(RiskSettings, 1)
    before_config = config_out(before) if before else None
    value = RiskSettings(id=1, **{**body.model_dump(), 'enabled': True})
    fingerprints = {}
    fixture = {'id': 1, 'title': '虚构示例：示例公司发布维护通知', 'summary': '示例公司计划例行维护。',
        'content': '本条为虚构测试，不是真实市场事件。示例公司计划例行维护，恢复时间尚未公布。'}
    try:
        provider, system, _, fingerprints['grouping'] = await configuration(session, value, 'grouping')
        grouped, _ = await complete(provider, system, {'article': fixture, 'candidates': []}, Grouping)
        for event in grouped.packages:
            if event.package_id is not None: raise ValueError('测试没有已有候选事件')
            validate_evidence([Evidence(article_id=1, quote=event.evidence)], {1: fixture})
        provider, system, _, fingerprints['analysis'] = await configuration(session, value, 'analysis')
        await summarize_articles(provider, system, [fixture], {'id': 1, 'title': fixture['title'],
            'fact': '示例公司计划例行维护', 'category': 'company', 'tags': ['维护'], 'content_kind': 'statement'})
        provider, system, _, fingerprints['outlook'] = await configuration(session, value, 'outlook')
        await briefing.generate(provider, system, [{'id': 1, 'revision': 1, 'importance_stars': 3, 'result': {'summary': '虚构测试'}}])
    except Exception as error:
        raise HTTPException(502, f'三阶段验证失败：{error}') from error
    previous = await session.get(RiskSettings, 1, populate_existing=True, with_for_update=True)
    if (config_out(previous) if previous else None) != before_config:
        raise HTTPException(409, '验证期间设置已变化，请重新操作')
    for stage, fingerprint in fingerprints.items():
        if (await configuration(session, value, stage))[3] != fingerprint:
            raise HTTPException(409, '验证期间模型或Skill已变化，请重新验证')
    value.start_at = previous.start_at if previous and previous.start_at else datetime.now(UTC)
    await session.merge(value); await session.commit()
    return success(None)


async def coverage(session):
    from alphapulse.news_risk.worker import eligible
    settings = await session.get(RiskSettings, 1)
    if not settings or not settings.start_at:
        return dict(total=0, done=0, failed=0, processing=0, pending=0)
    total = await session.scalar(select(func.count()).select_from(NewsItem).where(eligible(settings.start_at)))
    counts = dict((await session.execute(select(ArticleAnalysis.state, func.count()).join(NewsItem).where(
        ArticleAnalysis.content_hash == NewsItem.content_hash, eligible(settings.start_at)).group_by(ArticleAnalysis.state))).all())
    return {'total': total, **{k: counts.get(k, 0) for k in ('done', 'failed', 'processing')},
        'pending': max(0, total-sum(counts.values()))+counts.get('pending', 0)}


@router.get('/overview')
async def overview(session: DB, hours: RiskWindow = RiskWindow.DAY):
    now = datetime.now(UTC); cutoff = now - timedelta(hours=hours)
    counts = dict((await session.execute(select(NewsSummary.importance_stars, func.count()).where(
        NewsSummary.updated_at >= cutoff, NewsSummary.result.is_not(None), NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn').group_by(NewsSummary.importance_stars))).all())
    snapshot = await session.scalar(select(RiskSnapshot).where(RiskSnapshot.state == 'done', RiskSnapshot.result['inputs'].is_not(None)).order_by(RiskSnapshot.id.desc()).limit(1))
    last = await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
    scenario = None
    stage2 = dict((await session.execute(select(NewsSummary.state, func.count()).group_by(NewsSummary.state))).all())
    from alphapulse.news_risk.events import reading_inputs
    from alphapulse.news_risk.briefing import semantic_packages
    current = await reading_inputs(session, now-timedelta(hours=72))
    previous_inputs = semantic_packages(snapshot.result['inputs']) if snapshot else []
    changed = previous_inputs != semantic_packages(current)
    stage3 = {'state': last.state if last else 'pending', 'pending': changed,
        'heartbeat': await redis.get('ap:risk:outlook:heartbeat')}
    from alphapulse.models.risk import NewsEvent
    event_states = dict((await session.execute(select(NewsEvent.state, func.count()).group_by(NewsEvent.state))).all())
    return success({'event_states': event_states, 'stage2': stage2, 'stage3': stage3, 'as_of': now, 'counts': counts, 'coverage': await coverage(session), 'scenario': scenario, 'briefing': snapshot.result if snapshot else None,
        'snapshot_at': snapshot.created_at if snapshot else None, 'quotes': snapshot.quotes if snapshot else None,
        'stale': not snapshot or changed or bool(last and last.error),
        'error': last.error if last else None, 'heartbeat': await redis.get('ap:risk:heartbeat')})


@router.get('/summaries')
async def events(session: DB, hours: RiskWindow | None = None, stars: int = Query(0, ge=0, le=5),
                 tag: str = '', importance: int = Query(0, ge=0, le=5), category: str = '', asset: str = '', q: str = '', start: datetime | None = None, end: datetime | None = None,
                 page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    conditions = [NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn', NewsSummary.stars >= stars, NewsSummary.result.is_not(None)]
    if importance: conditions.append(NewsSummary.importance_stars >= importance)
    if tag: conditions.append(NewsSummary.tags.contains([tag.strip().casefold()]))
    if hours: conditions.append(NewsSummary.updated_at >= datetime.now(UTC)-timedelta(hours=hours))
    if start: conditions.append(NewsSummary.updated_at >= start)
    if end: conditions.append(NewsSummary.updated_at < end)
    if category: conditions.append(NewsSummary.category == category)
    if asset: conditions.append(NewsSummary.result['asset_impacts'].astext.contains(asset, autoescape=True))
    if q: conditions.append(NewsSummary.title.contains(q, autoescape=True))
    rows = (await session.scalars(select(NewsSummary).where(*conditions).order_by(
        NewsSummary.stars.desc(), NewsSummary.updated_at.desc(), NewsSummary.id.desc()).offset((page-1)*limit).limit(limit))).all()
    return success({'items': [await event_out(e, session) for e in rows], 'total': await session.scalar(select(func.count()).select_from(NewsSummary).where(*conditions))})


async def event_out(event, session):
    count = await session.scalar(select(func.count(func.distinct(ArticleAnalysis.news_id))).join(SummaryArticle).join(NewsItem).where(
        SummaryArticle.summary_id == event.id, SummaryArticle.active.is_(True), ArticleAnalysis.content_hash == NewsItem.content_hash))
    from alphapulse.models.risk import EventMember
    membership = await session.get(EventMember, event.id)
    return {'main_event_id': membership.event_id if membership else None, 'replacement_ids': event.replacement_ids, 'risk_stars': event.stars, 'article_count': count, **{key: getattr(event, key) for key in ('id', 'title', 'category', 'entities', 'tags', 'content_kind', 'importance_stars', 'peak_stars', 'result', 'published_at', 'updated_at', 'state', 'analyzed_at', 'revision', 'published_revision', 'error')}}


@router.get('/summaries/{event_id}')
async def event_detail(event_id: int, session: DB):
    event = await session.get(NewsSummary, event_id)
    if event is None: raise HTTPException(404, '风险事件不存在')
    rows = (await session.scalars(select(ArticleAnalysis).join(SummaryArticle).where(
        SummaryArticle.summary_id == event_id).order_by(ArticleAnalysis.id))).all()
    versions = (await session.scalars(select(SummaryVersion).where(SummaryVersion.summary_id == event_id)
        .order_by(SummaryVersion.id.desc()))).all()
    from alphapulse.models.risk import PackageEvidence
    evidence = (await session.scalars(select(PackageEvidence).where(PackageEvidence.summary_id == event_id).order_by(PackageEvidence.id))).all()
    from alphapulse.models.risk import GroupingVersion, PackageCorrection, EventLink
    grouping = (await session.scalars(select(GroupingVersion).where(GroupingVersion.analysis_id.in_([r.id for r in rows])).order_by(GroupingVersion.id.desc()))).all()
    corrections = (await session.scalars(select(PackageCorrection).where(PackageCorrection.input['packages'].contains([{'id':event_id}]) | PackageCorrection.result['package_ids'].contains([event_id])))).all()
    links = (await session.scalars(select(EventLink).where(EventLink.package_id == event_id))).all()
    return success({'related_events':[{'id':l.event_id,'reason':l.reason} for l in links], 'corrections':[{'id':c.id,'input':c.input,'result':c.result} for c in corrections], 'grouping_versions':[{'id':g.id,'state':g.state,'result':g.result,'error':g.error} for g in grouping], 'evidence': [{'id': e.id, 'version_id': e.version_id, 'kind': e.kind, 'data': e.data, 'fetched_at': e.fetched_at} for e in evidence], 'event': await event_out(event, session), 'history': [analysis_out(r) for r in rows],
        'versions': [{'id': v.id, 'revision': v.revision, 'state': v.state, 'model': v.model,
        'rules': v.rules, 'result': v.result, 'error': v.error, 'analyzed_at': v.finished_at} for v in versions]})


def analysis_out(row):
    return {'id': row.id, 'news_id': row.news_id, 'state': row.state, 'result': None, 'input': {k: v for k, v in row.input.items() if k != 'content'},
            'model': row.model, 'error': row.error, 'analyzed_at': row.finished_at,
            'historical': row.started_at is not None and (row.started_at-datetime.fromisoformat(row.input['progress_at'])).total_seconds() > 300}


@router.get('/tasks')
async def analyses(session: DB, stage: str = Query('grouping', pattern='^(grouping|analysis|events|outlook)$'), news_id: int | None = None, revision_no: int | None = Query(None, ge=1), failed: bool = False):
    if stage == 'events':
        from alphapulse.models.risk import EventVersion
        query = select(EventVersion)
        if failed: query = query.where(EventVersion.state == 'failed')
        rows = (await session.scalars(query.order_by(EventVersion.id.desc()).limit(100))).all()
        return success([{'id':r.id, 'state':r.state, 'error':r.error, 'model':r.rules.get('model_config', {}).get('model'), 'input':{'title':f'事件时间线 #{r.event_id}'}, 'result':None} for r in rows])
    if stage == 'outlook':
        query = select(RiskSnapshot)
        if failed: query = query.where(RiskSnapshot.state == 'failed')
        rows = (await session.scalars(query.order_by(RiskSnapshot.id.desc()).limit(100))).all()
        return success([{'id': r.id, 'state': r.state, 'error': r.error, 'model': r.model, 'input': {'title': '市场综合判断'}, 'result': None} for r in rows])
    if stage == 'analysis':
        query = select(SummaryVersion)
        if failed: query = query.where(SummaryVersion.state == 'failed')
        rows = (await session.scalars(query.order_by(SummaryVersion.id.desc()).limit(100))).all()
        return success([{'id': r.id, 'summary_id': r.summary_id, 'state': r.state, 'error': r.error, 'model': r.model,
            'input': {'title': f'事件总结 #{r.summary_id}'}, 'result': r.result} for r in rows])
    query = select(ArticleAnalysis)
    if news_id: query = query.where(ArticleAnalysis.news_id == news_id)
    if revision_no is not None:
        if not news_id: raise HTTPException(400, '指定修订版本时必须提供新闻 ID')
        current = await session.get(NewsItem, news_id)
        if current is None: raise HTTPException(404, '新闻不存在')
        content_hash = current.content_hash if current.revision_no == revision_no else await session.scalar(
            select(NewsRevision.content_hash).where(NewsRevision.news_id == news_id, NewsRevision.revision_no == revision_no))
        if content_hash is None: raise HTTPException(404, '新闻修订版本不存在')
        query = query.where(ArticleAnalysis.content_hash == content_hash)
    if failed: query = query.where(ArticleAnalysis.state == 'failed')
    return success([analysis_out(r) for r in (await session.scalars(query.order_by(ArticleAnalysis.id.desc()).limit(100))).all()])


@router.post('/tasks/{analysis_id}/retry', status_code=202)
async def retry(analysis_id: int, session: DB, stage: str = Query('grouping', pattern='^(grouping|analysis|events|outlook)$')):
    if stage == 'events':
        from alphapulse.models.risk import NewsEvent, EventVersion
        row = await session.get(EventVersion, analysis_id, with_for_update=True)
        if row is None or row.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
        event = await session.get(NewsEvent, row.event_id, with_for_update=True)
        if event.revision != row.revision or event.published_revision >= row.revision or event.state in ('pending','processing'):
            raise HTTPException(409, '事件已有新版本或正在处理')
        event.state = 'pending'; event.due_at = datetime.now(UTC); event.error = None
        await session.commit(); return success(None)
    if stage == 'outlook':
        row = await session.get(RiskSnapshot, analysis_id, with_for_update=True)
        if row is None or row.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
        row.quotes = {**row.quotes, 'retry_requested': True}
        await session.commit(); return success(None)
    if stage == 'analysis':
        row = await session.get(SummaryVersion, analysis_id, with_for_update=True)
        if row is None or row.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
        event = await session.get(NewsSummary, row.summary_id, with_for_update=True)
        if event.revision != row.revision:
            raise HTTPException(409, '已有更新的事件输入，请重试最新任务')
        if event.published_revision >= row.revision:
            raise HTTPException(409, '该修订已有成功结果，无需重试旧失败任务')
        if event.state in ('pending', 'processing'):
            raise HTTPException(409, '该包已排队或正在处理')
        event.state = 'pending'; event.due_at = datetime.now(UTC); event.error = None
        await session.commit(); return success(None)
    row = await session.get(ArticleAnalysis, analysis_id, with_for_update=True)
    if row is None: raise HTTPException(404, '分析记录不存在')
    if row.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
    current = await session.get(NewsItem, row.news_id)
    if current.content_hash != row.content_hash:
        raise HTTPException(409, '新闻已修订，请等待最新版本分析；旧记录保留')
    row.state = 'pending'; row.error = None
    await session.commit()
    return success(None)


@router.get('/news/{news_id}/summaries')
async def news_summaries(news_id: int, session: DB, revision_no: int | None = Query(None, ge=1)):
    query = select(NewsSummary).join(SummaryArticle).join(ArticleAnalysis).where(ArticleAnalysis.news_id == news_id, SummaryArticle.active.is_(True))
    if revision_no is not None:
        query = query.where(ArticleAnalysis.input['revision_no'].as_integer() == revision_no)
    return success([await event_out(e, session) for e in (await session.scalars(query.distinct())).all()])


@router.get('/tags')
async def tags(session: DB, q: str = ''):
    rows = (await session.scalars(select(NewsSummary.tags))).all()
    return success(sorted({tag for row in rows for tag in row if q.casefold() in tag}))


@router.get('/briefings')
async def briefings(session: DB, page: int = Query(1, ge=1)):
    rows = (await session.scalars(select(RiskSnapshot).where(RiskSnapshot.result['inputs'].is_not(None))
        .order_by(RiskSnapshot.id.desc()).offset((page-1)*20).limit(20))).all()
    return success([{'id': r.id, 'state': r.state, 'result': r.result, 'error': r.error, 'created_at': r.created_at} for r in rows])


@router.get('/events')
async def event_list(session: DB, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    from alphapulse.models.risk import NewsEvent
    rows = (await session.scalars(select(NewsEvent).where(NewsEvent.state != 'retired')
        .order_by(NewsEvent.updated_at.desc(), NewsEvent.id).offset((page-1)*limit).limit(limit))).all()
    return success({'items': [{'id': r.id, 'title': r.title, 'state': r.state, 'result': r.result,
        'revision': r.revision, 'error': r.error, 'updated_at': r.updated_at} for r in rows],
        'total': await session.scalar(select(func.count()).select_from(NewsEvent).where(NewsEvent.state != 'retired'))})


@router.get('/events/{identifier}')
async def event_timeline(identifier: int, session: DB):
    from alphapulse.models.risk import NewsEvent, EventMember, EventVersion
    event = await session.get(NewsEvent, identifier)
    if event is None: raise HTTPException(404, '事件不存在')
    packages = (await session.scalars(select(NewsSummary).join(EventMember, EventMember.package_id == NewsSummary.id)
        .where(EventMember.event_id == identifier))).all()
    versions = (await session.scalars(select(EventVersion).where(EventVersion.event_id == identifier).order_by(EventVersion.id.desc()))).all()
    return success({'id': event.id, 'title': event.title, 'state': event.state, 'result': event.result, 'error': event.error,
        'packages': [await event_out(p, session) for p in packages],
        'versions': [{'id': v.id, 'revision': v.revision, 'state': v.state, 'result': v.result, 'error': v.error, 'rules': v.rules} for v in versions]})


class ReprocessIn(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode='after')
    def range_valid(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError('请提供带时区的有效起止时间')
        return self


@router.post('/reprocess/preview')
async def reprocess_preview(body: ReprocessIn, session: DB):
    from alphapulse.news_risk.reprocess import preview
    counts, _ = await preview(session, body.start, body.end)
    return success(counts)


@router.post('/reprocess', status_code=202)
async def reprocess_start(body: ReprocessIn, session: DB):
    from alphapulse.news_risk.reprocess import start_job
    try: result = await start_job(session, body.start, body.end)
    except ValueError as error: raise HTTPException(409, str(error)) from error
    return success(result)


@router.get('/reprocess')
async def reprocess_list(session: DB):
    from alphapulse.models.risk import ReprocessJob
    rows = (await session.scalars(select(ReprocessJob).order_by(ReprocessJob.id.desc()).limit(20))).all()
    return success([{'id': r.id, 'state': r.state, 'total': len(r.items), 'completed': r.position, 'failures': r.failures,
        'created_at': r.created_at} for r in rows])


@router.post('/reprocess/{identifier}/cancel')
async def reprocess_cancel(identifier: int, session: DB):
    from alphapulse.models.risk import ReprocessJob
    job = await session.get(ReprocessJob, identifier, with_for_update=True)
    if job is None: raise HTTPException(404, '任务不存在')
    if job.state not in ('pending', 'running'): raise HTTPException(409, '任务已经结束')
    job.state = 'cancelled'; await session.commit(); return success(None)


@router.post('/reprocess/{identifier}/retry', status_code=202)
async def reprocess_retry(identifier: int, session: DB):
    from sqlalchemy import text
    from alphapulse.models.risk import ReprocessJob
    await session.execute(text('SELECT pg_advisory_xact_lock(20261101, 2)'))
    job = await session.get(ReprocessJob, identifier, with_for_update=True)
    if job is None or job.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
    if await session.scalar(select(ReprocessJob.id).where(ReprocessJob.state.in_(['pending', 'running'])).limit(1)):
        raise HTTPException(409, '已有重整理任务')
    outlook_ids = {f['id'] for f in job.failures if f['stage'] == 'outlook'}
    snapshots = (await session.scalars(select(RiskSnapshot).where(RiskSnapshot.id.in_(outlook_ids), RiskSnapshot.state == 'failed',
        RiskSnapshot.quotes['job_id'].as_integer() == job.id))).all()
    for snapshot in snapshots: snapshot.quotes = {**snapshot.quotes, 'retry_requested': True}
    from alphapulse.models.risk import NewsEvent
    for stage, model in (('analysis', NewsSummary), ('events', NewsEvent)):
        ids = {f['id'] for f in job.failures if f['stage'] == stage}
        rows = (await session.scalars(select(model).where(model.id.in_(ids)).order_by(model.id).with_for_update())).all()
        for row in rows:
            if row.state == 'failed':
                row.state = 'pending'; row.due_at = datetime.now(UTC); row.error = None
    ids = {f['id'] for f in job.failures if f['stage'] == 'grouping'}
    if ids:
        job.items = [i for i in job.items if i['id'] in ids]
        job.position = 0
    else:
        job.position = len(job.items)
    job.failures = []; job.state = 'running'
    await session.commit(); return success(None)
