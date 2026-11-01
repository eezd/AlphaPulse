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
    complete = bool(article and article.result and packages and all(p.result and p.published_revision == p.revision for p in packages))
    return complete, article, packages


async def preview(session, start, end, modes):
    linked = select(NewsSummary.id).join(SummaryArticle).where(
        SummaryArticle.analysis_id == ArticleAnalysis.id, SummaryArticle.active.is_(True),
        NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn')
    incomplete = linked.where(or_(NewsSummary.result.is_(None), func.jsonb_typeof(NewsSummary.result) == 'null',
        NewsSummary.published_revision != NewsSummary.revision))
    complete = exists(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id == NewsItem.id,
        ArticleAnalysis.content_hash == NewsItem.content_hash, ArticleAnalysis.result.is_not(None),
        func.jsonb_typeof(ArticleAnalysis.result) != 'null', exists(linked), ~exists(incomplete)))
    rows = (await session.execute(select(NewsItem.id, NewsItem.content_hash, complete.label('complete')).where(
        NewsItem.published_at >= start, NewsItem.published_at < end, NewsItem.published_at <= datetime.now(UTC))
        .order_by(NewsItem.published_at, NewsItem.id))).all()
    counts = {'total':len(rows), 'missing':0, 'review':0}
    items = []
    for row in rows:
        mode = 'review' if row.complete else 'missing'
        counts[mode] += 1
        if mode in modes: items.append({'id':row.id, 'hash':row.content_hash, 'mode':mode})
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
    value = {'items':items, 'config':config, 'request':{'start':start.isoformat(),'end':end.isoformat(),'modes':modes}}
    await redis.set('ap:risk:preview:'+identifier, json.dumps(value, default=str), ex=900)
    return {**counts, 'preview_id':identifier, 'models':{k:{field:v['provider'][field] for field in ('model','reasoning_effort')} for k,v in config['configurations'].items()}}


async def start(session, identifier):
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
    job = ReprocessJob(state='pending', items=[], position=0, failures=[], settings=value['config'])
    session.add(job); await session.flush()
    job.settings = {**job.settings, 'job_id':job.id, 'request':value['request']}
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
            for package in packages:
                if package.result and package.published_revision == package.revision and mode == 'missing': continue
                previous = await session.scalar(select(ReprocessTarget).where(ReprocessTarget.job_id==job.id,
                    ReprocessTarget.kind=='analysis',ReprocessTarget.object_id==package.id))
                if previous is None or package.revision > previous.revision:
                    if package.state not in ('pending','processing'):
                        worker.schedule(package,datetime.now(UTC),True)
                    package.review_config = job.settings
                    await target(session,job.id,'analysis',package)
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
        obj = await session.get(NewsSummary if row.kind=='analysis' else NewsEvent,row.object_id)
        if obj is None: continue
        if row.kind=='analysis' and (obj.replacement_ids or obj.state=='withdrawn'):
            for replacement in obj.replacement_ids:
                linked=await session.get(NewsSummary,replacement)
                await target(session,job.id,'analysis',linked)
            if obj.replacement_ids:
                await session.delete(row); waiting=True
            continue
        if obj.state=='retired': continue
        if obj.state=='failed': failures.append({'stage':row.kind,'id':obj.id,'error':obj.error})
        elif row.kind == 'events' and await session.scalar(select(NewsSummary.id).join(EventMember).where(
            EventMember.event_id == obj.id, NewsSummary.state == 'failed', NewsSummary.replacement_ids == []).limit(1)):
            failures.append({'stage':'events','id':obj.id,'error':'组成包研判失败，请先重试失败包'})
        elif obj.state not in ('done','retired') or obj.published_revision < row.revision: waiting=True
        if row.kind=='analysis':
            membership=await session.get(EventMember,obj.id)
            if membership:
                event=await session.get(NewsEvent,membership.event_id)
                await target(session,job.id,'events',event)
                if not any(t.kind=='events' and t.object_id==event.id for t in targets): waiting=True
                if event.state=='failed': failures.append({'stage':'events','id':event.id,'error':event.error})
    if waiting: await session.commit(); return
    job.failures=list({(f['stage'],f['id']):f for f in failures}.values()); job.state='failed' if failures else 'done'
    await session.commit()
