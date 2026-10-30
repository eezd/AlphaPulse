"""Authenticated news risk research endpoints."""
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from alphapulse.api.response import success
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.models import NewsItem, NewsRevision
from alphapulse.models.risk import RiskSettings, RiskAnalysis, RiskEvent, RiskSnapshot
from alphapulse.news_risk.service import START, default_settings, provider_for, complete, Labels, SYSTEM

router = APIRouter(prefix='/api/risk', tags=['risk'], dependencies=[Depends(require_admin)])
DB = Annotated[AsyncSession, Depends(get_session)]


class RiskWindow(IntEnum):
    DAY = 24
    TWO_DAYS = 48
    WEEK = 168


class SettingsIn(BaseModel):
    enabled: bool = False
    provider_id: int = Field(gt=0)
    model: str = Field(min_length=1, max_length=128, pattern=r'.*\S.*')


@router.get('/settings')
async def settings(session: DB):
    value = await default_settings(session)
    return success({'enabled': value.enabled, 'provider_id': value.provider_id, 'model': value.model, 'start_at': START})


@router.put('/settings')
async def save_settings(body: SettingsIn, session: DB):
    settings = RiskSettings(id=1, **body.model_dump())
    try:
        await provider_for(session, settings)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if body.enabled:
        previous = await session.get(RiskSettings, 1)
        if not previous or not previous.enabled or previous.provider_id != body.provider_id or previous.model != body.model:
            raise HTTPException(409, '请先使用测试并启用按钮验证模型')
    await session.merge(settings); await session.commit()
    return success(None)


@router.post('/enable')
async def enable(body: SettingsIn, session: DB):
    settings = RiskSettings(id=1, **{**body.model_dump(), 'enabled': True})
    try:
        provider = await provider_for(session, settings)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    fixture = {'news': [{'id': 1, 'title': '虚构测试新闻：示例交易平台发布维护通知',
        'summary': '示例交易平台计划进行例行维护，期间暂停充提服务。',
        'content': '本条为虚构测试样例，不代表真实市场事件。示例交易平台计划进行例行维护，'
                   '期间暂停充提服务。通知未提及资产损失，恢复时间尚未公布。'}], 'candidates': []}
    from alphapulse.news_risk.service import validate_labels
    try:
        result, _ = await complete(provider, SYSTEM, fixture, Labels)
        validate_labels(result, {1: fixture['news'][0]}, set())
    except Exception as error:
        raise HTTPException(502, f'模型验证失败：{error}') from error
    await session.merge(settings); await session.commit()
    return success(None)


async def coverage(session):
    total = await session.scalar(select(func.count()).select_from(NewsItem).where(NewsItem.published_at.between(START, datetime.now(UTC))))
    counts = dict((await session.execute(select(RiskAnalysis.state, func.count()).join(NewsItem).where(
        RiskAnalysis.content_hash == NewsItem.content_hash,
        NewsItem.published_at.between(START, datetime.now(UTC))).group_by(RiskAnalysis.state))).all())
    return {'total': total, 'done': counts.get('done', 0), 'failed': counts.get('failed', 0),
            'processing': counts.get('processing', 0), 'pending': max(0, total - sum(counts.values())) + counts.get('pending', 0)}


@router.get('/overview')
async def overview(session: DB, hours: RiskWindow = RiskWindow.DAY):
    now = datetime.now(UTC); cutoff = now - timedelta(hours=hours)
    counts = dict((await session.execute(select(RiskEvent.stars, func.count()).where(
        RiskEvent.updated_at >= cutoff, RiskEvent.result["verification"].astext != "insufficient").group_by(RiskEvent.stars))).all())
    snapshot = await session.scalar(select(RiskSnapshot).where(RiskSnapshot.state == 'done').order_by(RiskSnapshot.id.desc()).limit(1))
    last = await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
    scenario = next((w for w in snapshot.result['windows'] if w['hours'] == hours), None) if snapshot else None
    return success({'as_of': now, 'counts': counts, 'coverage': await coverage(session), 'scenario': scenario,
        'snapshot_at': snapshot.created_at if snapshot else None, 'quotes': snapshot.quotes if snapshot else None,
        'stale': not snapshot or (now-snapshot.created_at).total_seconds() > 1200 or bool(last.error),
        'error': last.error if last else None, 'heartbeat': await redis.get('ap:risk:heartbeat')})


@router.get('/events')
async def events(session: DB, hours: RiskWindow | None = None, stars: int = Query(1, ge=0, le=5),
                 q: str = '', start: datetime | None = None, end: datetime | None = None,
                 page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    conditions = [RiskEvent.stars >= stars]
    if hours: conditions.append(RiskEvent.updated_at >= datetime.now(UTC)-timedelta(hours=hours))
    if start: conditions.append(RiskEvent.updated_at >= start)
    if end: conditions.append(RiskEvent.updated_at < end)
    if q: conditions.append(RiskEvent.title.contains(q, autoescape=True))
    rows = (await session.scalars(select(RiskEvent).where(*conditions).order_by(
        RiskEvent.stars.desc(), RiskEvent.updated_at.desc(), RiskEvent.id.desc()).offset((page-1)*limit).limit(limit))).all()
    return success({'items': [event_out(e) for e in rows], 'total': await session.scalar(select(func.count()).select_from(RiskEvent).where(*conditions))})


def event_out(event):
    return {key: getattr(event, key) for key in ('id', 'title', 'category', 'entities', 'stars', 'peak_stars', 'result', 'published_at', 'updated_at')}


@router.get('/events/{event_id}')
async def event_detail(event_id: int, session: DB):
    event = await session.get(RiskEvent, event_id)
    if event is None: raise HTTPException(404, '风险事件不存在')
    rows = (await session.scalars(select(RiskAnalysis).where(RiskAnalysis.event_id == event_id).order_by(RiskAnalysis.id))).all()
    return success({'event': event_out(event), 'history': [analysis_out(r) for r in rows]})


def analysis_out(row):
    return {'id': row.id, 'news_id': row.news_id, 'state': row.state, 'result': row.result, 'input': row.input,
            'model': row.model, 'error': row.error, 'analyzed_at': row.finished_at,
            'historical': row.started_at is not None and (row.started_at-datetime.fromisoformat(row.input['progress_at'])).total_seconds() > 300}


@router.get('/analyses')
async def analyses(session: DB, news_id: int | None = None, revision_no: int | None = Query(None, ge=1), failed: bool = False):
    query = select(RiskAnalysis)
    if news_id: query = query.where(RiskAnalysis.news_id == news_id)
    if revision_no is not None:
        if not news_id: raise HTTPException(400, '指定修订版本时必须提供新闻 ID')
        current = await session.get(NewsItem, news_id)
        if current is None: raise HTTPException(404, '新闻不存在')
        content_hash = current.content_hash if current.revision_no == revision_no else await session.scalar(
            select(NewsRevision.content_hash).where(NewsRevision.news_id == news_id, NewsRevision.revision_no == revision_no))
        if content_hash is None: raise HTTPException(404, '新闻修订版本不存在')
        query = query.where(RiskAnalysis.content_hash == content_hash)
    if failed: query = query.where(RiskAnalysis.state == 'failed')
    return success([analysis_out(r) for r in (await session.scalars(query.order_by(RiskAnalysis.id.desc()).limit(100))).all()])


@router.post('/analyses/{analysis_id}/retry', status_code=202)
async def retry(analysis_id: int, session: DB):
    row = await session.get(RiskAnalysis, analysis_id, with_for_update=True)
    if row is None: raise HTTPException(404, '分析记录不存在')
    if row.state != 'failed': raise HTTPException(409, '仅可重试失败任务')
    current = await session.get(NewsItem, row.news_id)
    if current.content_hash != row.content_hash:
        raise HTTPException(409, '新闻已修订，请等待最新版本分析；旧记录保留')
    row.state = 'pending'; row.error = None
    await session.commit()
    return success(None)
