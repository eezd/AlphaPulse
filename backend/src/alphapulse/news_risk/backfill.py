"""Snapshot-selected historical completion and explicit full rechecks."""
import json
import secrets
from datetime import UTC, datetime
from sqlalchemy import select, func, text, exists, or_, any_, literal, BigInteger
from sqlalchemy.dialects.postgresql import insert, ARRAY
from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import redis
from alphapulse.models import NewsItem
from alphapulse.models.risk import (RiskSettings, ReprocessJob, ReprocessItem, ReprocessTarget,
    ArticleAnalysis, SummaryArticle, NewsSummary, NewsEvent, EventMember)
from alphapulse.news_risk.reprocess import capture


async def classification(session, news):
    article = await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id == news.id, ArticleAnalysis.content_hash == news.content_hash))
    packages = [] if article is None else (await session.scalars(select(NewsSummary).join(SummaryArticle).where(
        SummaryArticle.analysis_id == article.id, SummaryArticle.active.is_(True), NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn'))).all()
    events = (await session.scalars(select(NewsEvent).join(EventMember).where(EventMember.package_id.in_([p.id for p in packages])))).all()
    complete = bool(article and article.result and packages and len(events)==len(packages) and all(e.result and e.state=='done' and e.published_revision==e.revision for e in events))
    return complete, article, packages


async def preview(session, start, end, modes, selection="range"):
    linked = select(NewsSummary.id).join(SummaryArticle).where(
        SummaryArticle.analysis_id == ArticleAnalysis.id, SummaryArticle.active.is_(True),
        NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn')
    finished_event = exists(select(NewsEvent.id).join(EventMember).where(EventMember.package_id==NewsSummary.id,
        NewsEvent.state=='done', func.jsonb_typeof(NewsEvent.result)=='object', NewsEvent.published_revision==NewsEvent.revision))
    incomplete = linked.where(~finished_event)
    complete = exists(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id == NewsItem.id,
        ArticleAnalysis.content_hash == NewsItem.content_hash, ArticleAnalysis.result.is_not(None),
        func.jsonb_typeof(ArticleAnalysis.result) != 'null', exists(linked), ~exists(incomplete)))
    selection_filter = exists(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id == NewsItem.id,
        or_(ArticleAnalysis.started_at.is_not(None), ArticleAnalysis.result.is_not(None)))) if selection == 'attempted' else (
        (NewsItem.published_at >= start) & (NewsItem.published_at < end))
    rows = (await session.execute(select(NewsItem.id, NewsItem.content_hash, complete.label('complete')).where(
        selection_filter, NewsItem.published_at <= datetime.now(UTC))
        .order_by(NewsItem.published_at, NewsItem.id))).all()
    counts = {'total':len(rows), 'missing':0, 'review':0}
    items = []
    for row in rows:
        mode = 'review' if row.complete else 'missing'
        counts[mode] += 1
        if selection == 'attempted' or mode in modes: items.append({'id':row.id, 'hash':row.content_hash, 'mode':'review' if selection == 'attempted' else mode})
    packages = select(NewsSummary.id).join(SummaryArticle).join(ArticleAnalysis).join(NewsItem).where(
        NewsItem.id == any_(literal([i['id'] for i in items],type_=ARRAY(BigInteger))), ArticleAnalysis.content_hash == NewsItem.content_hash,
        SummaryArticle.active.is_(True), NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn').distinct()
    counts['articles'] = len(items)
    counts['packages'] = await session.scalar(select(func.count()).select_from(packages.subquery()))
    counts['events'] = await session.scalar(select(func.count(func.distinct(EventMember.event_id))).where(EventMember.package_id.in_(packages)))
    settings = await session.get(RiskSettings, 1)
    if settings is None: raise ValueError('请先配置新闻AI')
    config = await capture(session, settings)
    identifier = secrets.token_urlsafe(24)
    value = {'items':items, 'config':config, 'request':{'start':start.isoformat() if start else None,'end':end.isoformat() if end else None,'modes':modes,'selection':selection}}
    await redis.set('ap:risk:preview:'+identifier, json.dumps(value, default=str), ex=900)
    return {**counts, 'preview_id':identifier, 'models':{k:{field:v['provider'][field] for field in ('model','reasoning_effort')} for k,v in config['configurations'].items()}}


async def prepare_start(session, identifier):
    raw = await redis.get('ap:risk:preview:'+identifier)
    if not raw: raise ValueError('预览已过期，请重新预览')
    value = json.loads(raw)
    await session.execute(text('SELECT pg_advisory_xact_lock(20261101, 2)'))
    if await session.scalar(select(ReprocessJob.id).where(ReprocessJob.state.in_(['pending','running']))):
        raise ValueError('已有回补任务')
    settings = await session.get(RiskSettings, 1)
    if settings is None or await capture(session, settings) != value['config']:
        raise ValueError('模型或Skill配置已变化，请重新预览')
    if not value['items']: raise ValueError('没有符合选择条件的新闻')
    return value


async def start(session, identifier, run_mode="shared", validated=False):
    value = await prepare_start(session, identifier)
    if run_mode == 'history_only':
        if not validated: raise ValueError('仅历史批次必须先验证三阶段配置')
        settings = await session.get(RiskSettings, 1)
        settings.enabled = True
        if settings.start_at is None: settings.start_at = datetime.now(UTC)
    job = ReprocessJob(state='pending', items=[], position=0, failures=[], settings=value['config'])
    session.add(job); await session.flush()
    job.settings = {**job.settings, 'job_id':job.id, 'request':{**value['request'], 'run_mode':run_mode}}
    session.add_all([ReprocessItem(job_id=job.id, news_id=i['id'], content_hash=i['hash'], mode=i['mode'], state='pending') for i in value['items']])
    await session.commit()
    await redis.delete('ap:risk:preview:'+identifier)
    return {'id':job.id}


async def target(session, job_id, kind, obj):
    await session.execute(insert(ReprocessTarget).values(job_id=job_id, kind=kind, object_id=obj.id, revision=obj.revision)
        .on_conflict_do_update(index_elements=['job_id','kind','object_id'], set_={'revision':obj.revision}))


async def process(identifier):
    from alphapulse.news_risk import worker
    async with SessionLocal() as session:
        item = await session.get(ReprocessItem, identifier, with_for_update=True)
        if item is None or item.state != 'pending': return
        job = await session.get(ReprocessJob, item.job_id, with_for_update=True)
        settings = await session.get(RiskSettings, 1)
        if job.state not in ('pending','running') or not settings.enabled: return
        job_id, news_id, mode, content_hash = item.job_id, item.news_id, item.mode, item.content_hash
        item.state = 'processing'; item.error = None; item.finished_at = None; item.started_at = datetime.now(UTC); job.state = 'running'
        await session.commit()
        try:
            news = await session.get(NewsItem, item.news_id)
            if news is None or news.content_hash != item.content_hash: raise ValueError('原文版本变化，请重新预览范围')
            complete, article, packages = await classification(session, news)
            if mode == 'missing' and complete:
                item.state = 'skipped'; item.finished_at = datetime.now(UTC); await session.commit(); return
            needs_grouping = mode == 'review' or not article or not article.result or not packages
            if article is None:
                article = ArticleAnalysis(news_id=news.id, content_hash=news.content_hash, state='reprocess', input={
                    'title':news.title,'summary':news.summary,'content':news.content_text,'url':news.url,'source':news.source_code,
                    'source_tags':news.tags,'revision_no':news.revision_no,'published_at':news.published_at.isoformat(),
                    'progress_at':max(news.published_at,news.source_updated_at or news.published_at).isoformat()})
                session.add(article); await session.flush()
            article_id = article.id
            await session.commit()
            if needs_grouping:
                outcome = await worker.analyze(article_id, job.settings)
                if outcome == 'deferred':
                    item.state = 'pending'; await session.commit(); return
                if outcome != 'done':
                    row = await session.get(ArticleAnalysis, article_id, populate_existing=True)
                    raise ValueError(row.error or '原文已变化，分包未完成')
            await session.rollback()
            job = await session.get(ReprocessJob, job_id, populate_existing=True)
            news = await session.get(NewsItem,news_id,populate_existing=True)
            if news.content_hash != content_hash: raise ValueError('原文版本变化，请重新预览范围')
            _, _, packages = await classification(session,news)
            event_ids = set(await session.scalars(select(EventMember.event_id).where(EventMember.package_id.in_([p.id for p in packages]))))
            for event_id in sorted(event_ids):
                event = await session.get(NewsEvent,event_id,with_for_update=True)
                if event.result and event.published_revision==event.revision and mode=='missing': continue
                previous = await session.scalar(select(ReprocessTarget).where(ReprocessTarget.job_id==job.id,
                    ReprocessTarget.kind=='events',ReprocessTarget.object_id==event.id))
                if previous is None or event.revision > previous.revision:
                    if event.state not in ('pending','processing'):
                        from alphapulse.news_risk.event_research import schedule
                        schedule(event,datetime.now(UTC),True)
                    event.review_config = job.settings
                    await target(session,job.id,'events',event)
            item = await session.get(ReprocessItem, identifier, populate_existing=True)
            item.state = 'done'; item.finished_at = datetime.now(UTC)
            await session.commit()
        except Exception as error:
            await session.rollback()
            item = await session.get(ReprocessItem,identifier)
            item.state='failed'; item.error=str(error)[:1500]; item.finished_at=datetime.now(UTC)
            await session.commit()


async def reconcile(session, job):
    job = await session.get(ReprocessJob,job.id,with_for_update={"key_share":True},populate_existing=True)
    if job.state not in ('pending','running'): return
    if await session.scalar(select(ReprocessItem.id).where(ReprocessItem.job_id==job.id,ReprocessItem.state.in_(['pending','processing'])).limit(1)): return
    items = (await session.scalars(select(ReprocessItem).where(ReprocessItem.job_id==job.id,ReprocessItem.state=='failed'))).all()
    targets = (await session.scalars(select(ReprocessTarget).where(ReprocessTarget.job_id==job.id))).all()
    failures = [{'stage':'grouping','id':i.news_id,'error':i.error} for i in items if i.state=='failed']
    waiting = False
    for row in targets:
        if row.kind != 'events': continue
        obj = await session.get(NewsEvent,row.object_id)
        if obj is None or obj.state=='retired': continue
        if obj.state=='failed': failures.append({'stage':'events','id':obj.id,'error':obj.error})
        elif obj.state!='done' or obj.published_revision < row.revision: waiting=True
    if waiting: await session.commit(); return
    if not failures and job.settings.get('request', {}).get('run_mode') == 'history_only':
        from alphapulse.models.risk import RiskSnapshot
        from alphapulse.news_risk import briefing
        from alphapulse.news_risk.events import reading_inputs
        from datetime import timedelta
        inputs = await reading_inputs(session, datetime.now(UTC)-timedelta(hours=72))
        key = briefing.fingerprint(briefing.semantic_packages(inputs), job.settings['configurations']['outlook']['rules'])
        snapshot = await session.scalar(select(RiskSnapshot).where(
            RiskSnapshot.quotes['job_id'].astext.cast(BigInteger) == job.id,
            RiskSnapshot.quotes['fingerprint'].as_string() == key).order_by(RiskSnapshot.id.desc()).limit(1))
        if snapshot is None or snapshot.state == 'processing' or snapshot.quotes.get('retry_requested'): return
        if snapshot.state == 'failed': failures.append({'stage':'outlook','id':snapshot.id,'error':snapshot.error})
    if job.settings.get('request', {}).get('run_mode') == 'history_only':
        settings = await session.get(RiskSettings, 1)
        settings.enabled = False
    job.failures=list({(f['stage'],f['id']):f for f in failures}.values()); job.state='failed' if failures else 'done'
    await session.commit()
