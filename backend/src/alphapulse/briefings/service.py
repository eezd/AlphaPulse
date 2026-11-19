"""原文冻结、首次入队与 claim 所有权；完成结果只追加版本。"""
import asyncio
from datetime import UTC, datetime, timedelta
from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.orm import load_only
from alphapulse.core.db import SessionLocal
from alphapulse.models.news import NewsItem, Source
from alphapulse.briefings import admission, analysis_execution, config, contracts, keywords, schedule
from alphapulse.briefings.models import Analysis, AnalysisPart, Report, ReportPart
from alphapulse.ai import progress

ACTIVE_REPORT_STATES = ('pending', 'waiting_backfill', 'waiting_coverage', 'processing')


def report_scope(configuration):
    return or_(Report.request_id.is_not(None), Report.report_date >= schedule.backfill_start(configuration))


def automatic_source(news_id=None):
    """来源和全文资格；手动窗口与版本号都不能绕过准入。"""
    return admission.analysis_allowed(news_id)


def news_scope(configuration):
    return and_(automatic_source(), admission.archive_time(effective_expression()) >= schedule.backfill_cutoff(configuration))


def manual_news_scope():
    # 只为尚未冻结资料的手动分报准入；日报通过其确定性分报依赖取得资料。
    effective = effective_expression()
    return and_(admission.report_news_allowed(), exists().where(Report.request_id.is_not(None), Report.kind.in_(schedule.PERIOD_KINDS),
                          Report.state.in_(ACTIVE_REPORT_STATES), Report.input.is_(None),
                          effective >= Report.window_start, effective < Report.window_end).correlate(NewsItem))


def analysis_scope(configuration):
    # 首次待处理版本按当前来源归窗；已领取/完成版本使用冻结时间，手动重试不受限。
    admitted = select(or_(news_scope(configuration), manual_news_scope())).where(
        NewsItem.id == Analysis.news_id).scalar_subquery()
    return and_(automatic_source(Analysis.news_id), or_(Analysis.version_no > 1, case((Analysis.state == 'pending', admitted),
               else_=Analysis.effective_at >= schedule.backfill_cutoff(configuration))))


def effective_expression():
    precision = func.coalesce(NewsItem.extra['published_at_precision'].as_string(),
                              NewsItem.extra['time_precision'].as_string())
    unreliable = NewsItem.extra['published_at_unreliable'].as_boolean()
    return case((or_(precision.in_(('date', 'day', 'month', 'unknown')), unreliable.is_(True)), NewsItem.first_seen_at),
                else_=func.coalesce(NewsItem.published_at, NewsItem.first_seen_at))


def article_snapshot(news):
    return {'id': news.id, 'news_id': news.id, 'source_code': news.source_code,
            'title': news.title, 'summary': news.summary, 'content_text': news.content_text or '',
            'url': news.url, 'published_at': news.published_at.isoformat() if news.published_at else None,
            'first_seen_at': news.first_seen_at.isoformat(), 'extra': news.extra,
            'revision_no': news.revision_no, 'content_hash': news.content_hash}


async def enroll(session, settings, now):
    """仅 worker 在 settings 锁下调用；GET 不调用，重复调度不重复首条分析。"""
    config.normalize_backfill(settings)
    news = list(await session.scalars(select(NewsItem).where(or_(news_scope(settings.config), manual_news_scope()),
                                     ~exists().where(Analysis.news_id == NewsItem.id))
                                     .order_by(NewsItem.first_seen_at.desc(), NewsItem.id).limit(500)))
    for item in news:
        at, basis = schedule.effective_time(item)
        session.add(Analysis(news_id=item.id, version_no=1, state='pending', effective_at=at, time_basis=basis,
                             lane='historical_backfill' if item.first_seen_at <= settings.enabled_at else 'live'))
    if settings.history_start is None:
        earliest = await session.scalar(select(func.min(effective_expression())).where(automatic_source()))
        first = (earliest or now).astimezone(schedule.SHANGHAI).date()
        settings.history_start = schedule.backfill_start(settings.config, first)
        settings.history_end = settings.enabled_at.astimezone(schedule.SHANGHAI).date()
        settings.history_cursor = settings.history_start
    days = []
    cursor = settings.history_cursor
    for _ in range(7):
        if cursor > now.astimezone(schedule.SHANGHAI).date():
            break
        days.append(cursor)
        cursor += timedelta(days=1)
    settings.history_cursor = cursor
    settings.history_enrolled = cursor > settings.history_end
    # 重启/跨日只补到期窗口；历史游标负责更早日期。
    today = now.astimezone(schedule.SHANGHAI).date()
    days += [today - timedelta(days=1), today]
    for day in sorted(set(days)):
        for kind in schedule.KINDS:
            if not schedule.includes_report(settings.config, kind, day):
                continue
            left, right, due = schedule.window(kind, day)
            if due > now:
                continue
            if await session.scalar(select(Report.id).where(Report.kind == kind, Report.report_date == day).limit(1)):
                continue
            lane = 'historical_backfill' if due <= settings.enabled_at else 'live'
            session.add(Report(kind=kind, report_date=day, version_no=1, lane=lane,
                               window_start=left, window_end=right, due_at=due, next_check_at=due))
    await session.flush()


async def owns(session, row, revision):
    await config.settings_row(session, lock=True)
    locked = await session.scalar(select(type(row)).where(type(row).id == row.id)
                                  .with_for_update().execution_options(populate_existing=True))
    # 维护暂停仅关闭新准入；只有显式撤销 claim 才阻止已发出请求发布。
    return locked is not None and locked.state == 'processing' and locked.control_revision == revision


async def revoke(session, reason='已暂停，任务已取消'):
    now = datetime.now(UTC)
    parents = {row.id: row for row in await session.scalars(
        select(Analysis).where(Analysis.state == 'processing').order_by(Analysis.id).with_for_update())}
    processing = list(await session.scalars(select(AnalysisPart).where(
        AnalysisPart.state.in_(('processing', 'waiting_repair')))
        .order_by(AnalysisPart.analysis_id, AnalysisPart.id).with_for_update()))
    for part in processing:
        if part.state == 'processing':
            parent = parents.get(part.analysis_id) or await session.get(Analysis, part.analysis_id, with_for_update=True)
            previous = dict(parent.usage or {})
            recorded = previous.get('rounds', [previous] if previous else [])
            previous['rounds'] = [*recorded, None]
            parent.usage = previous
            part.usage = {'rounds': [None]}
        part.state, part.error, part.finished_at = 'interrupted', reason, now
    for cls in (Analysis, Report, ReportPart):
        await session.execute(update(cls).where(cls.state == 'processing').values(state='interrupted', error=reason, finished_at=now))


async def recover(session):
    # 无法证明上次请求没有发送，重启不隐式重放。
    await revoke(session, '进程重启，原在途请求结果未知；请手动恢复')


async def process_analysis(identifier, revision):
    row = None
    usage = None
    try:
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            row = await session.scalar(select(Analysis).where(Analysis.id == identifier,
                                       analysis_scope(settings.config)).with_for_update())
            if not settings.enabled or settings.control_revision != revision or row is None or row.state != 'pending':
                return
            news = await session.get(NewsItem, row.news_id)
            if news is None:
                raise ValueError('新闻原文不存在')
            frozen_from = (row.rules or {}).get('frozen_from') if row.input is not None else None
            if frozen_from is not None:
                origin = await session.get(Analysis, frozen_from)
                if origin is None or origin.news_id != row.news_id or origin.input != row.input:
                    raise ValueError('冻结版本与原分析输入不一致')
            if frozen_from is None:
                row.effective_at, row.time_basis = schedule.effective_time(news)
            if (row.version_no == 1 and not await session.scalar(select(or_(news_scope(settings.config),
                    manual_news_scope())).where(NewsItem.id == news.id))):
                # 入队后来源可能修正时间；领取时再次检查自动范围或手动补报窗口。
                await session.commit()
                return
            provider, system, rules = await config.model_config(session, settings.config, 'analysis')
            payload = row.input if frozen_from is not None else article_snapshot(news)
            if frozen_from is None:
                payload['keyword_candidates'] = keywords.candidates(payload)
                if news.source_code in admission.RESEARCH_CODES:
                    payload['research_context'] = admission.research_context(news, await session.get(Source, news.source_code))
            rules = {**rules, 'keyword_lexicon': keywords.lexicon().metadata(),
                     **({'frozen_from': frozen_from} if frozen_from is not None else {})}
            row.input = payload
            row.content_hash, row.source_revision = payload['content_hash'], payload['revision_no']
            row.rules, row.state, row.control_revision = rules, 'processing', revision
            row.started_at = datetime.now(UTC)
            await session.commit()
            progress.bind_claim('analysis', row.id)
            progress.step('检查完整新闻', operation='单篇分析', current_news_id=news.id)
            admitted = await session.scalar(select(automatic_source()).where(NewsItem.id == news.id))
            await session.commit()
            if not admitted:
                raise ValueError('该来源当前仅采集，尚未接入知识分析')
            output, keyword_audit, usage = await analysis_execution.run(
                session, row, revision, provider, system, payload, rules, owns)
            progress.step('保存单篇分析', operation='保存结果')
            if not await owns(session, row, revision):
                return
            row.rules = {**row.rules, 'keyword_processing': keyword_audit}
            row.output, row.usage = output.model_dump(), usage
            row.keywords = [keyword.model_dump() for keyword in output.keywords]
            row.importance_score = output.importance_score
            row.importance_stars = contracts.stars(output.importance_score)
            row.state, row.error, row.finished_at = 'done', None, datetime.now(UTC)
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            locked = await session.scalar(select(Analysis).where(Analysis.id == identifier).with_for_update())
            if locked is not None and ((locked.state == 'processing' and locked.control_revision == revision)
                    or (locked.state == 'pending' and settings.control_revision == revision)):
                locked.state, locked.error, locked.finished_at = 'failed', str(error), datetime.now(UTC)
                locked.usage = getattr(error, 'usage', None) or usage or locked.usage
                await session.commit()


async def retry_analysis(session, identifier, *, frozen=False):
    settings = await config.settings_row(session, lock=True)
    old = await session.get(Analysis, identifier)
    if old is None:
        raise LookupError('新闻分析不存在')
    if frozen and (old.state not in ('failed', 'interrupted') or not old.input):
        raise ValueError('仅能沿用已冻结的失败或中断版本')
    if not await session.scalar(select(automatic_source()).where(NewsItem.id == old.news_id)):
        raise ValueError('该来源当前仅采集，尚未接入知识分析')
    active = await session.scalar(select(Analysis).where(Analysis.news_id == old.news_id,
                                  Analysis.state.in_(('pending', 'processing'))))
    if active is not None:
        if frozen:
            raise ValueError('已有其他分析在处理，请等待完成后再沿用冻结输入')
        in_scope = await session.scalar(select(news_scope(settings.config)).where(NewsItem.id == active.news_id))
        if active.state == 'processing' or active.version_no > 1 or in_scope:
            return active.id
        active.state, active.error, active.finished_at = 'interrupted', '由手动重试接替范围外首次分析', datetime.now(UTC)
        await session.flush()
    version = await session.scalar(select(func.max(Analysis.version_no)).where(Analysis.news_id == old.news_id))
    news = await session.get(NewsItem, old.news_id)
    if news is None:
        raise ValueError('新闻不存在')
    at, basis = (old.effective_at, old.time_basis) if frozen else schedule.effective_time(news)
    row = Analysis(news_id=old.news_id, version_no=version + 1, state='pending', lane='live',
                   effective_at=at, time_basis=basis,
                   input=old.input if frozen else None,
                   rules={'frozen_from': old.id} if frozen else None,
                   content_hash=old.content_hash if frozen else None,
                   source_revision=old.source_revision if frozen else None)
    session.add(row)
    await session.flush()
    return row.id


async def prepare_period(session, report, now, upstream=False, configuration=None):
    # 成功版本的时间已冻结；来源更正时间不能把旧分析移入另一份报告。
    # 同一次查询选择版本和时间，避免两次读取间有新成功版本而归窗不一致。
    latest = (select(Analysis.id).where(Analysis.news_id == NewsItem.id, Analysis.state == 'done')
              .order_by(Analysis.version_no.desc()).limit(1).correlate(NewsItem).scalar_subquery())
    effective = func.coalesce(Analysis.effective_at, effective_expression())
    source_fields = ('source_code', 'url', 'title', 'published_at', 'first_seen_at')
    rows = (await session.execute(select(NewsItem.id, Analysis,
        *[Analysis.input[key].as_string().label(key) for key in source_fields],
        Analysis.input['extra']['research'], Analysis.input['research_context'],
        Analysis.input['extra']['published_date'].as_string())
        .outerjoin(Analysis, Analysis.id == latest)
        .where(case((Analysis.id.is_(None), admission.report_news_allowed()), else_=admission.report_analysis_allowed()),
               effective >= report.window_start, effective < report.window_end, NewsItem.first_seen_at <= now)
        .options(load_only(Analysis.id, Analysis.news_id, Analysis.version_no, Analysis.effective_at,
                           Analysis.time_basis, Analysis.source_revision, Analysis.importance_stars, Analysis.output))
        .order_by(NewsItem.id))).all()
    ids = [identifier for identifier, _, *_ in rows]
    available = [(item, source) for _, item, *source in rows if item is not None]
    missing = sorted(set(ids) - {item.news_id for item, _ in available})
    if report.lane == 'historical_backfill' or report.request_id is not None:
        # 手动补报也等待首次准入/处理完成，不因全局范围外或实时 lane 提前耗尽缺失时钟。
        scope = (True if report.request_id is not None else news_scope(configuration or {}))
        first_version = (True if report.request_id is not None else Analysis.version_no == 1)
        outstanding = await session.scalar(select(NewsItem.id).where(NewsItem.id.in_(missing), scope,
            or_(~exists().where(Analysis.news_id == NewsItem.id), exists().where(and_(Analysis.news_id == NewsItem.id,
                first_version, Analysis.state.in_(('pending', 'processing')))))).limit(1)) if missing else None
        if outstanding is not None:
            report.state = 'waiting_backfill'
            report.first_check_at = None
            report.next_check_at = now + timedelta(seconds=15)
            return False
    first = report.first_check_at or (report.due_at if report.lane == 'live' and report.request_id is None else now)
    state, coverage, next_check = schedule.coverage_decision(len(ids), len(missing), now, first, upstream=upstream)
    coverage['missing_news_ids'] = missing
    report.coverage = coverage
    report.first_check_at = first
    if state != 'ready':
        report.state = state
        report.next_check_at = next_check or now
        report.error = coverage.get('reason')
        return False
    report.cutoff_at = now
    cards = [{'analysis_id': item.id, 'news_id': item.news_id, 'version_no': item.version_no,
              'effective_at': item.effective_at.isoformat(), 'time_basis': item.time_basis,
              'source_revision': item.source_revision,
              'source': {**dict(zip(source_fields, source[:5], strict=True)),
                         **({'research': {**source[5], 'published_date': source[7]}} if source[5] else {})},
              **({'research_context': source[6]} if source[6] else {}),
              'importance_stars': item.importance_stars, 'result': item.output} for item, source in available]
    cards.sort(key=lambda item: (item['effective_at'], item['news_id']))
    report.input = {'operation': 'period', 'analysis_ids': [item['analysis_id'] for item in cards],
                    'news_ids': ids, 'cards': cards, 'historical_backfill': report.lane == 'historical_backfill'}
    return True
