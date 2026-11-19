"""冻结报表资料和持久化有界生成步骤，不进行递归归纳。"""
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5
from sqlalchemy import func, select
from alphapulse.core.db import SessionLocal
from alphapulse.briefings import admission, config, contracts, market, schedule, service
from alphapulse.briefings.models import Analysis, Report, ReportPart
from alphapulse.ai import submission as model, progress


class BudgetBlocked(ValueError):
    pass


def fingerprint(stage, payload, rules, schema):
    value = {'stage': stage, 'input': payload, 'rules': rules, 'schema': schema.model_json_schema()}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


async def validate_admission(session, identifiers):
    identifiers = set(identifiers)
    admitted = set(await session.scalars(select(Analysis.id).where(
        Analysis.id.in_(identifiers), Analysis.state == 'done', admission.report_analysis_allowed()))) if identifiers else set()
    if admitted != identifiers:
        raise BudgetBlocked('报告包含当前未准入来源或仅建库资料，请重新补报对应分时窗口；历史报告保持不变')


async def prepare_daily(session, report, now):
    dependencies = []
    for kind in schedule.PERIOD_KINDS:
        item = await session.scalar(select(Report).where(Report.kind == kind, Report.report_date == report.report_date,
                    Report.state == 'done').order_by(Report.version_no.desc()).limit(1))
        if item is None:
            pending = await session.scalar(select(Report.id).where(Report.kind == kind,
                        Report.report_date == report.report_date, Report.state.in_(service.ACTIVE_REPORT_STATES)).limit(1))
            if (report.lane == 'historical_backfill' or report.request_id is not None) and pending is not None:
                report.state = 'waiting_backfill'
                report.first_check_at = None
                report.next_check_at = now + timedelta(seconds=15)
                return False
            first = report.first_check_at or (report.due_at if report.lane == 'live' and report.request_id is None else now)
            report.first_check_at = first
            report.error = '日报所需分时报告缺失'
            if now >= first + timedelta(hours=1):
                report.state = 'blocked'
            else:
                report.state = 'waiting_coverage'
                report.next_check_at = min(now + timedelta(minutes=15), first + timedelta(hours=1))
            report.coverage = {'reason': report.error, 'missing_report_kind': kind}
            return False
        dependencies.append(item)
    dependency_ids = sorted({identifier for item in dependencies for identifier in item.input['analysis_ids']})
    await validate_admission(session, dependency_ids)
    highlights = sorted({identifier for item in dependencies for identifier in item.output['highlight_analysis_ids']})
    if not set(highlights).issubset(dependency_ids):
        raise BudgetBlocked('日报重点引用不属于固定分时报告输入')
    if len(highlights) > 9:
        raise BudgetBlocked('日报重点原文超过9条，不发送模型请求')
    analyses = list(await session.scalars(select(Analysis).where(Analysis.id.in_(highlights)))) if highlights else []
    if len(analyses) != len(highlights) or any(item.input is None or item.state != 'done' for item in analyses):
        raise ValueError('日报重点原文版本不可用，不能改读最新版原文')
    coverage = {'total': sum(item.coverage.get('total', 0) for item in dependencies),
                'missing': sum(item.coverage.get('missing', 0) for item in dependencies),
                'periods': [{'report_id': item.id, 'kind': item.kind, 'coverage': item.coverage} for item in dependencies]}
    coverage.update(available=coverage['total'] - coverage['missing'],
                    missing_rate=coverage['missing'] / coverage['total'] if coverage['total'] else 0)
    report.coverage, report.cutoff_at = coverage, now
    report.input = {'operation': 'daily', 'historical_backfill': report.lane == 'historical_backfill',
        'dependencies': [{'report_id': item.id, 'kind': item.kind, 'version_no': item.version_no,
                          'output': item.output} for item in dependencies],
        'analysis_ids': sorted({identifier for item in dependencies for identifier in item.input['analysis_ids']}),
        'highlights': [{'analysis_id': item.id, 'news_id': item.news_id, 'source_revision': item.source_revision,
                        'article': item.input} for item in analyses]}
    return True


async def prepare_due(session, settings, now, *, upstream=()):
    selected = settings.config.get('analysis', {})
    analysis_failing = f"{selected.get('provider_id')}:{selected.get('model')}" in upstream
    rows = await session.scalars(select(Report).where(Report.state.in_(('pending', 'waiting_backfill', 'waiting_coverage')),
                           service.report_scope(settings.config), Report.next_check_at <= now).order_by(Report.lane.desc(), Report.due_at).limit(24).with_for_update())
    for report in rows:
        if report.input is not None:
            continue
        try:
            ready = (await prepare_daily(session, report, now) if report.kind == 'daily'
                     else await service.prepare_period(session, report, now, upstream=analysis_failing,
                                                       configuration=settings.config))
            if ready:
                report.market = await market.snapshot(session, report.window_start, report.window_end)
                report.state, report.error = 'pending', None
        except (ValueError, KeyError) as error:
            report.state, report.error = 'blocked', str(error)
        if report.state == 'blocked':
            from alphapulse.briefings.notifications import enqueue_report_alert
            await enqueue_report_alert(session, report)


async def run_part(report_id, revision, key, stage, payload, provider, system, rules, schema, regenerate, validator):
    digest = fingerprint(stage, payload, rules, schema)
    async with SessionLocal() as session:
        await config.settings_row(session, lock=True)
        report = await session.scalar(select(Report).where(Report.id == report_id).with_for_update())
        if report is None or report.state != 'processing' or report.control_revision != revision:
            raise asyncio.CancelledError()
        existing = await session.scalar(select(ReportPart).where(ReportPart.report_id == report_id, ReportPart.part_key == key))
        if existing is not None:
            if existing.input_hash != digest:
                raise ValueError('同一报表步骤的冻结输入已改变')
            if existing.state == 'done':
                result = schema.model_validate(existing.output)
                validator(result)
                return result
            raise ValueError('原步骤未成功，需创建补报版本')
        reused = None
        if not regenerate:
            reused = await session.scalar(select(ReportPart).where(ReportPart.input_hash == digest,
                    ReportPart.state == 'done', ReportPart.reused_from.is_(None)).order_by(ReportPart.id).limit(1))
        part = ReportPart(report_id=report_id, part_key=key, stage=stage, input_hash=digest, input=payload,
                          rules=rules, state='processing', control_revision=revision, started_at=datetime.now(UTC))
        session.add(part)
        await session.flush()
        identifier = part.id
        if reused is not None:
            result = schema.model_validate(reused.output)
            validator(result)
            part.output, part.reused_from, part.state = reused.output, reused.id, 'done'
            part.finished_at = datetime.now(UTC)
            await session.commit()
            return result
        await session.commit()
    progress.bind_claim('report_part', identifier)
    progress.step('生成报表步骤', operation=stage, report_id=report_id, part_key=key)
    usage = None
    try:
        if not model.fits_context(provider, system, payload, schema):
            raise BudgetBlocked('报表步骤超出上下文，未发送模型请求')
        with progress.model_request(provider):
            result, usage = await model.complete(provider, system, payload, schema)
        validator(result)
        async with SessionLocal() as session:
            part = await session.get(ReportPart, identifier)
            if not await service.owns(session, part, revision):
                raise asyncio.CancelledError()
            parent = await session.get(Report, report_id)
            if parent.state != 'processing':
                raise asyncio.CancelledError()
            part.state, part.output, part.usage, part.finished_at = 'done', result.model_dump(), usage, datetime.now(UTC)
            await session.commit()
        return result
    except asyncio.CancelledError:
        raise
    except Exception as error:
        async with SessionLocal() as session:
            part = await session.get(ReportPart, identifier)
            if await service.owns(session, part, revision):
                part.state, part.error, part.finished_at = 'failed', str(error), datetime.now(UTC)
                part.usage = getattr(error, 'usage', usage)
                await session.commit()
        raise
    finally:
        progress.bind_claim('report', report_id)


def header(report):
    return {'kind': report.kind, 'report_date': report.report_date.isoformat(),
            'window_start': report.window_start.isoformat(), 'window_end': report.window_end.isoformat(),
            'coverage': report.coverage, 'market': report.market,
            'regular_us_open': schedule.regular_us_open(report.report_date),
            'historical_backfill': report.lane == 'historical_backfill'}


def period_chunks(provider, system, base, cards):
    """先计划完整分片，再调用；单卡无法容纳或超过16片则零分片发送。"""
    chunks, chunk = [], []
    for card in cards:
        candidate = [*chunk, card]
        if model.fits_context(provider, system, {**base, 'operation': 'period_map', 'cards': candidate}, contracts.ReportResult):
            chunk = candidate
            continue
        if not chunk:
            raise BudgetBlocked('单篇资料卡超出分时报上下文')
        chunks.append(chunk)
        chunk = [card]
        if not model.fits_context(provider, system, {**base, 'operation': 'period_map', 'cards': chunk}, contracts.ReportResult):
            raise BudgetBlocked('单篇资料卡超出分时报上下文')
        if len(chunks) >= 16:
            raise BudgetBlocked('分时报所需分片超过16片，未开始分片请求')
    if chunk:
        chunks.append(chunk)
    return chunks


async def generate(report, revision, provider, system, rules):
    base = header(report)
    allowed = set(report.input['analysis_ids'])
    validate = lambda output: contracts.validate_references(output, allowed)
    async def part(key, stage, payload, schema=contracts.ReportResult, validator=validate):
        return await run_part(report.id, revision, key, stage, payload, provider, system, rules,
                              schema, report.regenerate, validator)
    if not allowed:
        return contracts.ReportResult(title=f'{report.report_date} {report.kind}（空资料）',
                summary='本窗口没有可用新闻；未调用模型。', sections=[], highlight_analysis_ids=[])
    if report.kind != 'daily':
        payload = {**base, 'operation': 'period', 'cards': report.input['cards']}
        if model.fits_context(provider, system, payload, contracts.ReportResult):
            return await part('final', 'period_final', payload)
        chunks = period_chunks(provider, system, base, report.input['cards'])
        summaries = []
        for index, cards in enumerate(chunks):
            ids = {card['analysis_id'] for card in cards}
            result = await part(f'map:{index}', 'period_map', {**base, 'operation': 'period_map', 'cards': cards},
                                validator=lambda output, ids=ids: contracts.validate_references(output, ids))
            summaries.append(result.model_dump())
        final = {**base, 'operation': 'period_reduce', 'parts': summaries}
        if not model.fits_context(provider, system, final, contracts.ReportResult):
            raise BudgetBlocked('分片摘要仍超出最终上下文；保留成功分片，不递归归纳')
        return await part('final', 'period_final', final)
    payload = {**base, **report.input}
    if model.fits_context(provider, system, payload, contracts.ReportResult):
        return await part('final', 'daily_final', payload)
    reviews = []
    for highlight in report.input['highlights']:
        identifier = highlight['analysis_id']
        def validate_highlight(output, identifier=identifier, article=highlight['article']):
            if output.analysis_id != identifier:
                raise ValueError('重点复核分析ID不匹配')
            contracts.validate_evidence(output, article)
        result = await part(f'highlight:{identifier}', 'highlight_review',
                    {'operation': 'highlight_review', **highlight}, contracts.HighlightReview, validate_highlight)
        reviews.append(result.model_dump())
    final = {**base, 'operation': 'daily', 'dependencies': report.input['dependencies'], 'reviews': reviews}
    if not model.fits_context(provider, system, final, contracts.ReportResult):
        raise BudgetBlocked('日报三报与重点复核仍超出上下文；不增加复核或递归请求')
    return await part('final', 'daily_final', final)


async def process_report(identifier, revision):
    try:
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            report = await session.scalar(select(Report).where(Report.id == identifier).with_for_update())
            if not settings.enabled or settings.control_revision != revision or report is None or report.state != 'pending' or report.input is None:
                return
            if report.request_id is None and not schedule.includes_report(settings.config, report.kind, report.report_date):
                return
            await validate_admission(session, report.input['analysis_ids'])
            provider, system, rules = await config.model_config(session, settings.config,
                                'daily' if report.kind == 'daily' else 'period')
            report.state, report.control_revision, report.rules = 'processing', revision, rules
            report.started_at = datetime.now(UTC)
            await session.commit()
            progress.bind_claim('report', identifier)
            progress.step('准备报表生成', operation='日报' if report.kind == 'daily' else '分时报告')
            result = await generate(report, revision, provider, system, rules)
            if not await service.owns(session, report, revision):
                return
            report.output, report.state, report.finished_at, report.error = result.model_dump(), 'done', datetime.now(UTC), None
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            report = await session.scalar(select(Report).where(Report.id == identifier).with_for_update())
            if report is not None and ((report.state == 'processing' and report.control_revision == revision)
                    or (report.state == 'pending' and settings.control_revision == revision)):
                report.state = 'blocked' if isinstance(error, BudgetBlocked) else 'failed'
                report.error, report.finished_at = str(error), datetime.now(UTC)
                from alphapulse.briefings.notifications import enqueue_report_alert
                await enqueue_report_alert(session, report)
                await session.commit()


async def request_report(session, kind, day, request_id, regenerate=False):
    settings = await config.settings_row(session, lock=True)
    existing = await session.scalar(select(Report).where(Report.request_id == request_id))
    if existing is not None:
        if existing.kind != kind or existing.report_date != day or existing.regenerate != regenerate:
            raise ValueError('请求ID已用于不同的报表请求')
        return existing
    active = await session.scalar(select(Report).where(Report.kind == kind, Report.report_date == day,
                                                      Report.state.in_(service.ACTIVE_REPORT_STATES)))
    if active is not None and (active.request_id is not None or active.state == 'processing'
                              or schedule.includes_report(settings.config, kind, day)):
        raise ValueError(f'该窗口已有进行中的报告 #{active.id}，请等待或查看其进度')
    now = datetime.now(UTC)
    left, right, due = schedule.window(kind, day)
    if now < due:
        raise ValueError('报告窗口尚未结束或尚未到延后5分钟的判断时间')
    if active is not None:
        # 先释放活动窗口，再追加手动版本；原冻结输入和步骤审计保留。
        active.state, active.error, active.finished_at = 'interrupted', '已由范围外手动补报接替', now
        await session.flush()
    version = await session.scalar(select(func.max(Report.version_no)).where(Report.kind == kind, Report.report_date == day))
    row = Report(kind=kind, report_date=day, version_no=(version or 0) + 1, request_id=request_id,
             regenerate=regenerate, window_start=left, window_end=right, due_at=due, next_check_at=now,
             first_check_at=now,
             lane='historical_backfill' if settings.enabled_at and due <= settings.enabled_at else 'live')
    session.add(row)
    await session.flush()
    if kind == 'daily':
        for period in schedule.PERIOD_KINDS:
            done = await session.scalar(select(Report.id).where(Report.kind == period, Report.report_date == day,
                                                               Report.state == 'done').limit(1))
            if done is not None:
                continue
            pending = await session.scalar(select(Report).where(Report.kind == period, Report.report_date == day,
                                                                Report.state.in_(service.ACTIVE_REPORT_STATES)))
            identifier = str(uuid5(UUID(request_id), period))
            if pending is not None:
                # 只固定准入意图，不重置在途请求、覆盖等待或已冻结的资料。
                if pending.request_id is None:
                    pending.request_id = identifier
            else:
                await request_report(session, period, day, identifier)
        await session.flush()
    return row
