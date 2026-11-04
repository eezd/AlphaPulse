"""Authenticated news risk research endpoints."""
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Annotated, Literal
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func, text, BigInteger
from sqlalchemy.ext.asyncio import AsyncSession
from alphapulse.ai.llm import Effort
from alphapulse.api.response import success
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.models import NewsItem, NewsRevision
from alphapulse.models.risk import RiskSettings, RiskSnapshot, ArticleAnalysis, NewsSummary, SummaryArticle, NewsEvent
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
    return {**{key: getattr(value, key) for key in SettingsIn.model_fields}, 'start_at': value.start_at, 'max_concurrency': value.max_concurrency if value.max_concurrency is not None else 5}


@router.get('/settings')
async def settings(session: DB):
    return success(config_out(await default_settings(session)))


@router.put('/settings')
async def save_settings(body: SettingsIn, session: DB):
    previous = await session.get(RiskSettings, 1, with_for_update=True)
    if body.enabled:
        raise HTTPException(409, '请先使用测试并启用按钮验证三阶段模型和Skills')
    value = RiskSettings(id=1, **body.model_dump(), start_at=previous.start_at if previous else None,
        max_concurrency=previous.max_concurrency if previous else 5)
    await session.merge(value); await session.commit()
    return success(None)


class ConcurrencyIn(BaseModel):
    max_concurrency: int = Field(gt=0, strict=True)


@router.patch('/settings/concurrency')
async def save_concurrency(body: ConcurrencyIn, session: DB):
    value = await default_settings(session)
    value.max_concurrency = body.max_concurrency
    session.add(value)
    await session.commit()
    return success({'max_concurrency': value.max_concurrency})


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
    from alphapulse.core.db import engine
    from alphapulse.news_risk.scheduler import GATE
    async with engine.connect() as gate:
        if not await gate.scalar(text('SELECT pg_try_advisory_lock(:gate, 1)'), {'gate': GATE}):
            raise HTTPException(409, '分析仍在运行或正在验证，请暂停并等待在途任务完成')
        await gate.commit()
        try:
            previous = await session.get(RiskSettings, 1, populate_existing=True)
            if previous and previous.enabled:
                raise HTTPException(409, '请先保存并暂停分析，再测试并启用')
            return await validate_enable(body, session)
        finally:
            await gate.execute(text('SELECT pg_advisory_unlock(:gate, 1)'), {'gate': GATE})
            await gate.commit()


async def validate_enable(body: SettingsIn, session: DB, activate=True):
    from alphapulse.news_risk.pipeline import configuration, Grouping, Evidence, validate_evidence
    from alphapulse.news_risk import briefing
    before = await session.get(RiskSettings, 1)
    before_config = {k: v for k, v in config_out(before).items() if k != 'max_concurrency'} if before else None
    value = RiskSettings(id=1, **{**body.model_dump(), 'enabled': True})
    fingerprints = {}
    fixture = {'id': 1, 'title': '虚构示例：示例公司发布维护通知', 'summary': '示例公司计划例行维护。',
        'content': '本条为虚构测试，不是真实市场事件。示例公司计划例行维护，恢复时间尚未公布。'}
    try:
        provider, system, _, fingerprints['grouping'] = await configuration(session, value, 'grouping')
        await session.commit()
        grouped, _ = await complete(provider, system, {'article': fixture, 'candidates': [], 'events': []}, Grouping)
        for event in grouped.packages:
            if event.package_id is not None or event.event.event_id is not None: raise ValueError('测试没有已有候选包或事件')
            validate_evidence([Evidence(article_id=1, quote=event.evidence)], {1: fixture})
        provider, system, _, fingerprints['analysis'] = await configuration(session, value, 'analysis')
        await session.commit()
        from alphapulse.news_risk.event_research import EventResult, validate
        package = {'id':1,'revision':1,'title':fixture['title'],'fact':'示例公司计划例行维护','article_ids':[1]}
        result, _ = await complete(provider,system,{'task':'event_research','id':1,
            'event':{'id':1,'revision':1,'title':fixture['title']},'packages':[package],'articles':[fixture]},EventResult)
        if result.id != 1 or result.correction is not None or result.package_correction is not None: raise ValueError('测试事件不得纠错或更换ID')
        validate(result,[package],{1:fixture})
        provider, system, _, fingerprints['outlook'] = await configuration(session, value, 'outlook')
        await session.commit()
        await briefing.generate(provider, system, [{'kind':'event','id': 1, 'revision': 1, 'importance_stars': 3, 'result': {'summary': '虚构测试'}}])
    except Exception as error:
        raise HTTPException(502, f'三阶段验证失败：{error}') from error
    previous = await session.get(RiskSettings, 1, populate_existing=True, with_for_update=True)
    current_config = {k: v for k, v in config_out(previous).items() if k != 'max_concurrency'} if previous else None
    if current_config != before_config:
        raise HTTPException(409, '验证期间设置已变化，请重新操作')
    for stage, fingerprint in fingerprints.items():
        if (await configuration(session, value, stage))[3] != fingerprint:
            raise HTTPException(409, '验证期间模型或Skill已变化，请重新验证')
    value.start_at = previous.start_at if previous and previous.start_at else datetime.now(UTC)
    value.max_concurrency = previous.max_concurrency if previous else 5
    if activate:
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
    level = NewsEvent.result['importance_stars'].as_integer()
    counts = dict((await session.execute(select(level, func.count()).where(
        NewsEvent.updated_at >= cutoff, func.jsonb_typeof(NewsEvent.result) == 'object', NewsEvent.state != 'retired').group_by(level))).all())
    snapshot = await session.scalar(select(RiskSnapshot).where(RiskSnapshot.state == 'done', func.jsonb_typeof(RiskSnapshot.result['inputs']) == 'array').order_by(RiskSnapshot.id.desc()).limit(1))
    last = await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
    scenario = None
    stage2 = dict((await session.execute(select(NewsEvent.state, func.count()).group_by(NewsEvent.state))).all())
    from alphapulse.news_risk.events import reading_inputs
    from alphapulse.news_risk.briefing import semantic_packages
    current = await reading_inputs(session, now-timedelta(hours=72))
    previous_inputs = semantic_packages(snapshot.result['inputs']) if snapshot else []
    changed = previous_inputs != semantic_packages(current)
    stage3 = {'state': last.state if last else 'pending', 'pending': changed,
        'heartbeat': await redis.get('ap:risk:outlook:heartbeat')}
    event_states = dict((await session.execute(select(NewsEvent.state, func.count()).group_by(NewsEvent.state))).all())
    import json
    runtime = await redis.get('ap:risk:concurrency')
    settings = await session.get(RiskSettings, 1)
    concurrency = {'limit': settings.max_concurrency if settings else 5, 'running': None, 'stages': {}, 'updated_at': None}
    if runtime: concurrency.update(json.loads(runtime))
    from alphapulse.models.risk import ReprocessJob
    jobs = (await session.scalars(select(ReprocessJob).where(ReprocessJob.state.in_(['pending', 'running'])))).all()
    from alphapulse.models.risk import ReprocessItem
    historical_pending = sum(i['state'] == 'pending' for job in jobs for i in job.items)
    historical_pending += await session.scalar(select(func.count()).select_from(ReprocessItem).where(ReprocessItem.job_id.in_([j.id for j in jobs if j.settings.get('request')]), ReprocessItem.state=='pending')) or 0
    coverage_data = await coverage(session)
    concurrency['queued'] = historical_pending + sum(stage2.get(k, 0) for k in ('pending',)) + coverage_data['pending']
    return success({'concurrency': concurrency, 'event_states': event_states, 'stage2': stage2, 'stage3': stage3, 'as_of': now, 'counts': counts, 'coverage': coverage_data, 'scenario': scenario, 'briefing': snapshot.result if snapshot else None,
        'snapshot_at': snapshot.created_at if snapshot else None, 'quotes': snapshot.quotes if snapshot else None,
        'stale': not snapshot or changed or bool(last and last.error),
        'error': last.error if last else None, 'heartbeat': await redis.get('ap:risk:heartbeat')})


@router.get('/packages')
async def packages(session: DB, tag: str = '', q: str = '', page: int = Query(1,ge=1), limit: int = Query(20,ge=1,le=100)):
    conditions = [NewsSummary.replacement_ids==[],NewsSummary.state!='withdrawn']
    if tag: conditions.append(NewsSummary.tags.contains([tag.strip().casefold()]))
    if q: conditions.append(NewsSummary.title.contains(q,autoescape=True))
    rows = (await session.scalars(select(NewsSummary).where(*conditions).order_by(NewsSummary.updated_at.desc(),NewsSummary.id)
        .offset((page-1)*limit).limit(limit))).all()
    return success({'items':[await package_out(p,session) for p in rows],
        'total':await session.scalar(select(func.count()).select_from(NewsSummary).where(*conditions))})


async def package_out(event, session):
    count = await session.scalar(select(func.count(func.distinct(ArticleAnalysis.news_id))).join(SummaryArticle).join(NewsItem).where(
        SummaryArticle.summary_id == event.id, SummaryArticle.active.is_(True), ArticleAnalysis.content_hash == NewsItem.content_hash))
    from alphapulse.models.risk import EventMember
    membership = await session.get(EventMember, event.id)
    return {'main_event_id': membership.event_id if membership else None, 'replacement_ids': event.replacement_ids,
        'fact':event.fact, 'article_count':count, **{key:getattr(event,key) for key in
        ('id','title','category','entities','tags','content_kind','published_at','updated_at','state','revision','error')}}



@router.get('/packages/{event_id}')
async def event_detail(event_id: int, session: DB):
    event = await session.get(NewsSummary, event_id)
    if event is None: raise HTTPException(404, '风险事件不存在')
    rows = (await session.scalars(select(ArticleAnalysis).join(SummaryArticle).where(
        SummaryArticle.summary_id == event_id).order_by(ArticleAnalysis.id))).all()
    from alphapulse.models.risk import GroupingVersion, PackageCorrection
    grouping = (await session.scalars(select(GroupingVersion).where(GroupingVersion.analysis_id.in_([r.id for r in rows])).order_by(GroupingVersion.id.desc()))).all()
    corrections = (await session.scalars(select(PackageCorrection).where(PackageCorrection.input['packages'].contains([{'id':event_id}]) | PackageCorrection.result['package_ids'].contains([event_id])))).all()
    return success({'corrections':[{'id':c.id,'input':c.input,'result':c.result} for c in corrections],
        'grouping_versions':[{'id':g.id,'state':g.state,'result':g.result,'error':g.error} for g in grouping],
        'event':await package_out(event,session),'history':[analysis_out(r) for r in rows]})


def analysis_out(row):
    return {'id': row.id, 'news_id': row.news_id, 'state': row.state, 'result': None, 'input': {k: v for k, v in row.input.items() if k != 'content'},
            'model': row.model, 'error': row.error, 'analyzed_at': row.finished_at,
            'historical': row.started_at is not None and (row.started_at-datetime.fromisoformat(row.input['progress_at'])).total_seconds() > 300}


@router.get('/tasks')
async def analyses(session: DB, stage: str = Query('grouping', pattern='^(grouping|events|outlook)$'), news_id: int | None = None, revision_no: int | None = Query(None, ge=1), failed: bool = False):
    if stage == 'events':
        from alphapulse.models.risk import EventVersion
        query = select(EventVersion)
        if failed: query = query.where(EventVersion.state == 'failed')
        rows = (await session.scalars(query.order_by(EventVersion.id.desc()).limit(100))).all()
        return success([{'id':r.id, 'state':r.state, 'error':r.error, 'model':r.rules.get('model_config', {}).get('model'), 'input':{'title':f'事件研判 #{r.event_id}'}, 'result':None} for r in rows])
    if stage == 'outlook':
        query = select(RiskSnapshot)
        if failed: query = query.where(RiskSnapshot.state == 'failed')
        rows = (await session.scalars(query.order_by(RiskSnapshot.id.desc()).limit(100))).all()
        return success([{'id': r.id, 'state': r.state, 'error': r.error, 'model': r.model, 'input': {'title': '市场综合判断'}, 'result': None} for r in rows])
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
async def retry(analysis_id: int, session: DB, stage: str = Query('grouping', pattern='^(grouping|events|outlook)$')):
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
    row = await session.get(ArticleAnalysis, analysis_id, with_for_update=True)
    if row is None: raise HTTPException(404, '分析记录不存在')
    if row.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
    current = await session.get(NewsItem, row.news_id)
    if current.content_hash != row.content_hash:
        raise HTTPException(409, '新闻已修订，请等待最新版本分析；旧记录保留')
    row.state = 'pending'; row.error = None
    await session.commit()
    return success(None)


@router.get('/news/{news_id}/packages')
async def news_summaries(news_id: int, session: DB, revision_no: int | None = Query(None, ge=1)):
    query = select(NewsSummary).join(SummaryArticle).join(ArticleAnalysis).where(ArticleAnalysis.news_id == news_id, SummaryArticle.active.is_(True))
    if revision_no is not None:
        query = query.where(ArticleAnalysis.input['revision_no'].as_integer() == revision_no)
    return success([await package_out(e, session) for e in (await session.scalars(query.distinct())).all()])


@router.get('/tags')
async def tags(session: DB, q: str = ''):
    rows = (await session.scalars(select(NewsSummary.tags))).all()
    return success(sorted({tag for row in rows for tag in row if q.casefold() in tag}))


@router.get('/briefings')
async def briefings(session: DB, page: int = Query(1, ge=1)):
    rows = (await session.scalars(select(RiskSnapshot).where(RiskSnapshot.result['inputs'].is_not(None))
        .order_by(RiskSnapshot.id.desc()).offset((page-1)*20).limit(20))).all()
    return success([{'id': r.id, 'state': r.state, 'result': r.result, 'error': r.error, 'created_at': r.created_at} for r in rows])


async def researched_event_out(event, session):
    from alphapulse.models.risk import EventMember
    count = await session.scalar(select(func.count()).select_from(EventMember).where(EventMember.event_id==event.id))
    return {**{k:getattr(event,k) for k in ('id','title','state','result','error','revision','published_revision','updated_at','category','tags','due_at')},
        'package_count':count, 'risk_stars':(event.result or {}).get('risk_stars',0),
        'importance_stars':(event.result or {}).get('importance_stars',0)}


@router.get('/events')
async def event_list(session: DB, hours: RiskWindow | None = None, stars: int = Query(0,ge=0,le=5),
        importance: int = Query(0,ge=0,le=5), tag: str = '', category: str = '', asset: str = '', q: str = '',
        start: datetime | None = None, end: datetime | None = None,
        page: int = Query(1,ge=1), limit: int = Query(20,ge=1,le=100)):
    conditions = [NewsEvent.state!='retired']
    if stars: conditions.append(NewsEvent.result['risk_stars'].as_integer()>=stars)
    if importance: conditions.append(NewsEvent.result['importance_stars'].as_integer()>=importance)
    if tag: conditions.append(NewsEvent.tags.contains([tag.strip().casefold()]))
    if category: conditions.append(NewsEvent.category==category)
    if asset: conditions.append(NewsEvent.result['asset_impacts'].astext.contains(asset,autoescape=True))
    if q: conditions.append(NewsEvent.title.contains(q,autoescape=True))
    if hours: conditions.append(NewsEvent.updated_at>=datetime.now(UTC)-timedelta(hours=hours))
    if start: conditions.append(NewsEvent.updated_at>=start)
    if end: conditions.append(NewsEvent.updated_at<end)
    rows = (await session.scalars(select(NewsEvent).where(*conditions).order_by(
        NewsEvent.result['importance_stars'].as_integer().desc().nullslast(),NewsEvent.updated_at.desc(),NewsEvent.id)
        .offset((page-1)*limit).limit(limit))).all()
    return success({'items':[await researched_event_out(e,session) for e in rows],
        'total':await session.scalar(select(func.count()).select_from(NewsEvent).where(*conditions))})


@router.get('/events/{identifier}')
async def event_timeline(identifier: int, session: DB):
    from alphapulse.models.risk import NewsEvent, EventMember, EventVersion, EventEvidence
    event = await session.get(NewsEvent, identifier)
    if event is None: raise HTTPException(404, '事件不存在')
    packages = (await session.scalars(select(NewsSummary).join(EventMember, EventMember.package_id == NewsSummary.id)
        .where(EventMember.event_id == identifier))).all()
    versions = (await session.scalars(select(EventVersion).where(EventVersion.event_id == identifier).order_by(EventVersion.id.desc()))).all()
    evidence = (await session.scalars(select(EventEvidence).where(EventEvidence.version_id.in_([v.id for v in versions])).order_by(EventEvidence.id))).all()
    return success({**await researched_event_out(event,session),
        'evidence':[{'id':e.id,'version_id':e.version_id,'data':e.data,'fetched_at':e.fetched_at} for e in evidence],
        'packages': [await package_out(p, session) for p in packages],
        'versions': [{'id': v.id, 'revision': v.revision, 'state': v.state, 'result': v.result, 'error': v.error, 'rules': v.rules, 'usage':v.usage,'created_at':v.created_at} for v in versions]})


class ReprocessIn(BaseModel):
    modes: list[Literal['missing','review']] = Field(min_length=1,max_length=2)
    start: datetime | None = None
    end: datetime | None = None
    selection: Literal['range', 'attempted'] = 'range'

    @model_validator(mode='after')
    def range_valid(self):
        if len(set(self.modes)) != len(self.modes): raise ValueError('回补选项不能重复')
        if self.selection == 'attempted': return self
        if self.start is None or self.end is None: raise ValueError('时间段不能为空')
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError('请提供带时区的有效起止时间')
        return self


@router.post('/reprocess/preview')
async def reprocess_preview(body: ReprocessIn, session: DB):
    from alphapulse.news_risk.backfill import preview
    try: return success(await preview(session,body.start,body.end,body.modes,body.selection))
    except ValueError as error: raise HTTPException(409,str(error)) from error


class ReprocessStart(BaseModel):
    run_mode: Literal['shared', 'history_only'] = 'shared'
    preview_id: str = Field(min_length=20,max_length=80,pattern='^[A-Za-z0-9_-]+$')


@router.post('/reprocess', status_code=202)
async def reprocess_start(body: ReprocessStart, session: DB):
    from alphapulse.news_risk.backfill import start, prepare_start
    if body.run_mode == 'history_only':
        from alphapulse.core.db import engine
        from alphapulse.news_risk.scheduler import GATE
        async with engine.connect() as gate:
            if not await gate.scalar(text('SELECT pg_try_advisory_lock(:gate, 1)'), {'gate': GATE}):
                raise HTTPException(409, '请先暂停AI并等待在途任务完成')
            await gate.commit()
            try:
                settings = await default_settings(session)
                if settings.enabled: raise HTTPException(409, '请先暂停AI')
                try: await prepare_start(session, body.preview_id)
                except ValueError as error: raise HTTPException(409, str(error)) from error
                # Enable and job creation commit together while admission is blocked.
                await validate_enable(SettingsIn(**config_out(settings)), session, activate=False)
                try: result = await start(session, body.preview_id, body.run_mode, validated=True)
                except ValueError as error: raise HTTPException(409, str(error)) from error
            finally:
                await gate.execute(text('SELECT pg_advisory_unlock(:gate, 1)'), {'gate': GATE})
                await gate.commit()
    else:
        try: result = await start(session, body.preview_id, body.run_mode)
        except ValueError as error: raise HTTPException(409, str(error)) from error
    return success(result)


@router.get('/reprocess')
async def reprocess_list(session: DB):
    from alphapulse.models.risk import ReprocessJob
    rows = (await session.scalars(select(ReprocessJob).order_by(ReprocessJob.id.desc()).limit(20))).all()
    from alphapulse.news_risk.queue import job_out
    return success([await job_out(session,r) for r in rows])


@router.post('/reprocess/{identifier}/cancel')
async def reprocess_cancel(identifier: int, session: DB):
    from alphapulse.models.risk import ReprocessJob
    job = await session.get(ReprocessJob, identifier, with_for_update=True)
    if job is None: raise HTTPException(404, '任务不存在')
    if job.state not in ('pending', 'running'): raise HTTPException(409, '任务已经结束')
    job.state = 'cancelled'
    if job.settings.get('request', {}).get('run_mode') == 'history_only':
        settings = await session.get(RiskSettings, 1)
        settings.enabled = False
    await session.commit(); return success(None)


@router.post('/reprocess/{identifier}/retry', status_code=202)
async def reprocess_retry(identifier: int, session: DB):
    from sqlalchemy import text
    from alphapulse.models.risk import ReprocessJob
    await session.execute(text('SELECT pg_advisory_xact_lock(20261101, 2)'))
    job = await session.get(ReprocessJob, identifier, with_for_update=True)
    if job is None or job.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
    if await session.scalar(select(ReprocessJob.id).where(ReprocessJob.state.in_(['pending', 'running'])).limit(1)):
        raise HTTPException(409, '已有重整理任务')
    if job.settings.get('request'):
        from alphapulse.models.risk import ReprocessItem
        from sqlalchemy import update
        await session.execute(update(ReprocessItem).where(ReprocessItem.job_id==job.id,ReprocessItem.state=='failed').values(state='pending',error=None))
    outlook_ids = {f['id'] for f in job.failures if f['stage'] == 'outlook'}
    snapshots = (await session.scalars(select(RiskSnapshot).where(RiskSnapshot.id.in_(outlook_ids), RiskSnapshot.state == 'failed',
        RiskSnapshot.quotes['job_id'].astext.cast(BigInteger) == job.id))).all()
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
        job.items = [{**i, 'state': 'pending'} for i in job.items if i['id'] in ids]
        job.position = 0
    else:
        job.position = len(job.items)
    job.failures = []; job.state = 'running'
    await session.commit(); return success(None)


@router.get('/queue')
async def queue(session: DB, state: Literal['running','queued','waiting','failed','done'] | None = None,
                stage: Literal['grouping','events','outlook','backfill'] | None = None,
                origin: Literal['history','realtime'] | None = None, job_id: int | None = Query(None,gt=0),
                page: int = Query(1,ge=1), limit: int = Query(20,ge=1,le=100)):
    from alphapulse.news_risk.queue import listing
    return success(await listing(session,state,stage,origin,job_id,page,limit))


@router.get('/reprocess/{identifier}')
async def reprocess_detail(identifier: int, session: DB, page: int = Query(1,ge=1), limit: int = Query(20,ge=1,le=100)):
    from alphapulse.models.risk import ReprocessJob, ReprocessItem
    from alphapulse.news_risk.queue import job_out
    job=await session.get(ReprocessJob,identifier)
    if job is None: raise HTTPException(404,'回补批次不存在')
    rows=(await session.execute(select(ReprocessItem,NewsItem.title,NewsItem.url).join(NewsItem).where(
        ReprocessItem.job_id==identifier).order_by(ReprocessItem.id).offset((page-1)*limit).limit(limit))).all()
    return success({**await job_out(session,job),'items':[{'id':i.id,'news_id':i.news_id,'title':title,'url':url,
        'mode':i.mode,'state':i.state,'error':i.error} for i,title,url in rows]})
