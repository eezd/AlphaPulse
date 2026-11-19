"""单篇新闻与定时报表的管理接口；读操作不入队付费任务。"""
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from alphapulse.ai.skills import SkillError
from alphapulse.api.response import success
from alphapulse.core.auth import require_admin
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import get_session
from alphapulse.models import NewsItem
from alphapulse.briefings import config, contracts, queue, reports, schedule, service
from alphapulse.briefings.models import Analysis, AnalysisPart, Notification, Report, ReportPart
from alphapulse.briefings.queries import keyword_match, usage_totals

router = APIRouter(prefix='/api/briefings', tags=['briefings'], dependencies=[Depends(require_admin)])
DB = Annotated[AsyncSession, Depends(get_session)]
Kind = Literal['morning', 'evening', 'intraday', 'daily']


class RevisionInput(contracts.StrictModel):
    base_revision: int = Field(ge=0)


class GenerateInput(contracts.StrictModel):
    kind: Kind
    report_date: date
    request_id: UUID
    regenerate: bool = False


class TelegramInput(RevisionInput):
    enabled: bool
    chat_id: str | None = Field(default=None, max_length=100)
    token: str | None = Field(default=None, min_length=1, max_length=256)
    clear_token: bool = False


def check_revision(row, value):
    if row.control_revision != value:
        raise HTTPException(409, '设置已变化，请重新读取后操作')


def telegram_view(row):
    chat = row.telegram_chat_id
    return {'enabled': row.telegram_enabled, 'token_configured': bool(row.telegram_token),
            'chat_id_masked': ('…' + chat[-4:]) if chat else None}


def settings_view(row):
    start = schedule.backfill_start(row.config)
    selected = {key: value for key, value in row.config.items()
                if key not in ('period_backfill_start', 'daily_backfill_start')}
    selected['backfill_start'] = start.isoformat() if start != date.min else None
    return {'enabled': row.enabled, 'config': selected, 'control_revision': row.control_revision,
        'enabled_at': row.enabled_at, 'history_start': row.history_start, 'history_end': row.history_end,
        'history_cursor': row.history_cursor, 'history_enrolled': row.history_enrolled,
        'timezone': 'Asia/Shanghai', 'telegram': telegram_view(row)}


@router.get('/settings')
async def settings(session: DB):
    row = await config.settings_row(session)
    await session.commit()
    return success(settings_view(row))


@router.put('/settings')
async def save(body: config.SettingsInput, session: DB):
    row = await config.settings_row(session, lock=True)
    check_revision(row, body.base_revision)
    selected = body.model_dump(mode='json', exclude={'base_revision'})
    try:
        await config.validate_config(session, selected)
    except (ValueError, KeyError) as error:
        raise HTTPException(422, str(error)) from error
    if ('backfill_start' not in row.config
            or schedule.backfill_start(row.config) != schedule.backfill_start(selected)):
        row.history_start = row.history_end = row.history_cursor = None
        row.history_enrolled = False
    row.config, row.enabled = selected, False
    row.control_revision += 1
    await service.revoke(session)
    await session.commit()
    return success(settings_view(row))


@router.post('/enable')
async def enable(body: RevisionInput, session: DB):
    row = await config.settings_row(session, lock=True)
    check_revision(row, body.base_revision)
    try:
        await config.validate_config(session, row.config)
        if row.telegram_enabled and (not row.telegram_token or not row.telegram_chat_id):
            raise ValueError('运行异常通知已启用，请先配置 Telegram Bot 与接收聊天')
    except (ValueError, KeyError) as error:
        raise HTTPException(422, str(error)) from error
    config.normalize_backfill(row)
    if row.enabled_at is None:
        row.enabled_at = datetime.now(UTC)
    row.enabled = True
    row.control_revision += 1
    await session.commit()
    return success(settings_view(row))


@router.post('/pause')
async def pause(body: RevisionInput, session: DB):
    row = await config.settings_row(session, lock=True)
    check_revision(row, body.base_revision)
    row.enabled = False
    row.control_revision += 1
    await service.revoke(session)
    await session.commit()
    return success(settings_view(row))


@router.get('/skill-templates')
async def skill_templates():
    return success([{'stage': stage, 'name': name, 'content': content}
                    for stage, (name, content) in contracts.SKILL_TEMPLATES.items()])


@router.post('/skill-templates/initialize')
async def initialize_skill_templates(body: RevisionInput, session: DB):
    row = await config.settings_row(session, lock=True)
    check_revision(row, body.base_revision)
    try:
        result = await config.initialize_skills(session)
    except SkillError as error:
        raise HTTPException(409, str(error)) from error
    await session.commit()
    return success(result)


@router.get('/telegram')
async def telegram(session: DB):
    row = await config.settings_row(session)
    await session.commit()
    return success({**telegram_view(row), 'control_revision': row.control_revision})


@router.put('/telegram')
async def save_telegram(body: TelegramInput, session: DB):
    row = await config.settings_row(session, lock=True)
    check_revision(row, body.base_revision)
    if body.clear_token and body.token:
        raise HTTPException(422, '不能同时清除和替换 token')
    if body.token is not None:
        import re
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', body.token):
            raise HTTPException(422, 'Telegram Bot token 格式不合法')
        row.telegram_token = encrypt(body.token).decode()
    elif body.clear_token:
        row.telegram_token = None
    if body.chat_id is not None:
        row.telegram_chat_id = body.chat_id.strip() or None
    if body.enabled and (not row.telegram_token or not row.telegram_chat_id):
        raise HTTPException(422, '启用运行异常通知需要 Bot token 和接收聊天')
    row.telegram_enabled = body.enabled
    row.control_revision += 1
    await session.commit()
    return success({**telegram_view(row), 'control_revision': row.control_revision})


@router.get('/coverage')
async def coverage(session: DB):
    row = await config.settings_row(session)
    news_total = await session.scalar(select(func.count()).select_from(NewsItem).where(service.news_scope(row.config)))
    unenrolled = await session.scalar(select(func.count()).select_from(NewsItem).where(service.news_scope(row.config),
        ~exists().where(Analysis.news_id == NewsItem.id)))
    analyses = [{'state': state, 'lane': lane, 'count': count} for state, lane, count in await session.execute(
        select(Analysis.state, Analysis.lane, func.count()).where(service.analysis_scope(row.config))
        .group_by(Analysis.state, Analysis.lane))]
    report_counts = [{'state': state, 'lane': lane, 'count': count} for state, lane, count in await session.execute(
        select(Report.state, Report.lane, func.count()).where(service.report_scope(row.config))
        .group_by(Report.state, Report.lane))]
    earliest = await session.scalar(select(func.min(service.effective_expression())))
    at = row.enabled_at or datetime.now(UTC)
    first = earliest.astimezone(schedule.SHANGHAI).date() if earliest else at.astimezone(schedule.SHANGHAI).date()
    first = schedule.backfill_start(row.config, first)
    last = at.astimezone(schedule.SHANGHAI).date()
    expected = 0
    from datetime import timedelta
    cursor = first
    while cursor <= last:
        expected += sum(schedule.includes_report(row.config, kind, cursor) and schedule.window(kind, cursor)[2] <= at
                        for kind in schedule.KINDS)
        cursor += timedelta(days=1)
    await session.commit()
    return success({'news_total': news_total, 'not_enrolled': unenrolled, 'analyses': analyses,
        'reports': report_counts, 'historical_windows_expected': expected, 'history_start': first,
        'history_end': last, 'history_cursor': row.history_cursor, 'history_enrolled': row.history_enrolled})


def analysis_view(row, news=None, *, detail=False):
    result = {key: getattr(row, key) for key in ('id', 'news_id', 'version_no', 'state', 'lane',
        'source_revision', 'effective_at', 'time_basis', 'importance_score', 'importance_stars',
        'keywords', 'error', 'created_at', 'started_at', 'finished_at')}
    result.update(title=(row.input or {}).get('title') or (news.title if news else '原文不可用'),
        source_changed=bool(news and row.source_revision is not None and row.source_revision != news.revision_no),
        result=row.output, rules=row.rules, usage=row.usage)
    if detail:
        result['input'] = row.input
    return result


@router.get('/analyses')
async def analyses(session: DB, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
                   news_id: int | None = None, state: str | None = None, q: str | None = None,
                   stars: int | None = Query(None, ge=1, le=5), region: str | None = None):
    conditions = []
    if news_id is not None:
        conditions.append(Analysis.news_id == news_id)
    if state:
        conditions.append(Analysis.state == state)
    if stars is not None:
        conditions.append(Analysis.importance_stars >= stars)
    if region:
        conditions.append(Analysis.output['primary_region']['code'].as_string() == region)
    if q:
        title = func.coalesce(func.nullif(Analysis.input['title'].as_string(), ''),
            select(NewsItem.title).where(NewsItem.id == Analysis.news_id).scalar_subquery())
        literal = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append(or_(title.ilike(f'%{literal}%', escape='\\'), keyword_match(Analysis.keywords, q)))
    if news_id is None:
        newest = select(Analysis.news_id, func.max(Analysis.version_no).label('version')).group_by(Analysis.news_id).subquery()
        conditions.append(exists(select(1).select_from(newest).where(newest.c.news_id == Analysis.news_id,
                          newest.c.version == Analysis.version_no)))
    total = await session.scalar(select(func.count()).select_from(Analysis).where(*conditions))
    rows = list(await session.scalars(select(Analysis).where(*conditions).order_by(Analysis.effective_at.desc(), Analysis.id.desc())
                         .offset((page - 1) * limit).limit(limit)))
    items = {item.id: item for item in await session.scalars(select(NewsItem).where(NewsItem.id.in_([r.news_id for r in rows])))}
    return success({'items': [analysis_view(item, items.get(item.news_id)) for item in rows], 'total': total})


@router.get('/analyses/{identifier}')
async def analysis(identifier: int, session: DB):
    row = await session.get(Analysis, identifier)
    if row is None:
        raise HTTPException(404, '新闻分析不存在')
    versions = list(await session.scalars(select(Analysis).where(Analysis.news_id == row.news_id).order_by(Analysis.version_no.desc())))
    parts = list(await session.scalars(select(AnalysisPart).where(AnalysisPart.analysis_id == identifier)
                                       .order_by(AnalysisPart.stage, AnalysisPart.part_index)))
    return success({**analysis_view(row, await session.get(NewsItem, row.news_id), detail=True),
        'parts': [{'stage': part.stage, 'part_index': part.part_index, 'state': part.state,
                   'start_offset': part.start_offset, 'end_offset': part.end_offset,
                   'reused_from': part.reused_from, 'usage': part.usage, 'error': part.error}
                  for part in parts],
        'versions': [{'id': item.id, 'version_no': item.version_no, 'state': item.state} for item in versions]})


@router.post('/analyses/{identifier}/retry')
async def retry_analysis(identifier: int, session: DB, frozen: bool = False):
    try:
        result = await service.retry_analysis(session, identifier, frozen=frozen)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    await session.commit()
    return success({'id': result})


def report_view(row, *, detail=False):
    result = {key: getattr(row, key) for key in ('id', 'kind', 'report_date', 'version_no', 'lane', 'state',
        'window_start', 'window_end', 'due_at', 'next_check_at', 'first_check_at', 'cutoff_at', 'coverage',
        'error', 'created_at', 'started_at', 'finished_at')}
    result['title'] = (row.output or {}).get('title')
    if detail:
        result.update(input=row.input, output=row.output, market=row.market, rules=row.rules)
    else:
        result['summary'] = (row.output or {}).get('summary')
    return result


@router.get('/reports')
async def list_reports(session: DB, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
        kind: Kind | None = None, start: date | None = None, end: date | None = None,
        region: str | None = None, stars: int | None = Query(None, ge=1, le=5), q: str | None = None):
    conditions = []
    if kind:
        conditions.append(Report.kind == kind)
    if start:
        conditions.append(Report.report_date >= start)
    if end:
        conditions.append(Report.report_date <= end)
    # 通过冻结分析 ID 过滤，包含报告引用但不一定在正文重复陈述的资料。
    if region or stars is not None:
        matching = select(Analysis.id).where(Report.input['analysis_ids'].contains(func.jsonb_build_array(Analysis.id)))
        if region:
            matching = matching.where(Analysis.output['primary_region']['code'].as_string() == region)
        if stars is not None:
            matching = matching.where(Analysis.importance_stars >= stars)
        conditions.append(matching.correlate(Report).exists())
    if q:
        matching = select(Analysis.id).where(Report.input['analysis_ids'].contains(func.jsonb_build_array(Analysis.id)),
                                            keyword_match(Analysis.keywords, q))
        literal = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append(or_(Report.output['title'].as_string().ilike(f'%{literal}%', escape='\\'),
                              matching.correlate(Report).exists()))
    total = await session.scalar(select(func.count()).select_from(Report).where(*conditions))
    rows = await session.scalars(select(Report).where(*conditions).order_by(Report.report_date.desc(), Report.kind, Report.version_no.desc())
                                 .offset((page - 1) * limit).limit(limit))
    return success({'items': [report_view(row) for row in rows], 'total': total})


@router.get('/reports/{identifier}')
async def report(identifier: int, session: DB):
    row = await session.get(Report, identifier)
    if row is None:
        raise HTTPException(404, '报告不存在')
    parts = list(await session.scalars(select(ReportPart).where(ReportPart.report_id == row.id).order_by(ReportPart.id)))
    versions = await session.scalars(select(Report).where(Report.kind == row.kind, Report.report_date == row.report_date)
                                    .order_by(Report.version_no.desc()))
    sources = list(await session.scalars(select(Analysis).where(Analysis.id.in_((row.input or {}).get('analysis_ids', [])))))
    return success({**report_view(row, detail=True), 'usage': usage_totals(parts),
        'parts': [{key: getattr(part, key) for key in ('id', 'part_key', 'stage', 'state', 'input_hash',
                   'reused_from', 'usage', 'error', 'started_at', 'finished_at')} for part in parts],
        'versions': [{'id': item.id, 'version_no': item.version_no, 'state': item.state} for item in versions],
        'sources': [{'analysis_id': item.id, 'news_id': item.news_id, 'source_revision': item.source_revision,
                     'title': (item.input or {}).get('title'), 'url': (item.input or {}).get('url')} for item in sources]})


@router.post('/reports/generate')
async def generate(body: GenerateInput, session: DB):
    try:
        row = await reports.request_report(session, body.kind, body.report_date, str(body.request_id), body.regenerate)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    await session.commit()
    return success({'id': row.id, 'version_no': row.version_no, 'state': row.state})


@router.get('/queue')
async def ai_queue(session: DB):
    return success(await queue.queue_view(session))


@router.post('/queue/reset-waits')
async def reset_waits(body: queue.ResetWaits, session: DB):
    return success(await queue.reset_waits(session, body))


@router.get('/notifications')
async def notifications(session: DB, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    total = await session.scalar(select(func.count()).select_from(Notification))
    rows = await session.scalars(select(Notification).order_by(Notification.id.desc()).offset((page - 1) * limit).limit(limit))
    return success({'total': total, 'items': [{key: getattr(row, key) for key in ('id', 'incident', 'state', 'message',
        'attempts', 'error', 'created_at', 'next_attempt_at', 'sent_at')} for row in rows]})
