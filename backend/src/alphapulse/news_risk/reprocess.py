"""Explicit bounded historical jobs, sharing the normal news pipeline."""
from sqlalchemy import select, text, update
from alphapulse.core.db import SessionLocal
from alphapulse.models import NewsItem
from alphapulse.models.risk import RiskSettings, ReprocessJob, ReprocessItem, ArticleAnalysis, NewsSummary, EventMember
from alphapulse.news_risk.pipeline import configuration


async def preview(session, start, end):
    rows = (await session.scalars(select(NewsItem).where(NewsItem.published_at >= start, NewsItem.published_at < end)
        .order_by(NewsItem.published_at, NewsItem.id))).all()
    from alphapulse.models.risk import SummaryArticle
    ids = [r.id for r in rows]
    packages = set((await session.scalars(select(SummaryArticle.summary_id).join(ArticleAnalysis)
        .where(ArticleAnalysis.news_id.in_(ids), SummaryArticle.active.is_(True)))).all())
    events = set((await session.scalars(select(EventMember.event_id).where(EventMember.package_id.in_(packages)))).all())
    return {'articles': len(rows), 'packages': len(packages), 'events': len(events)}, [{'id': r.id, 'hash': r.content_hash} for r in rows]


async def capture(session, settings):
    from alphapulse.api.risk import config_out
    value = config_out(settings)
    value.pop('start_at')
    value.pop('enabled', None)
    value.pop('max_concurrency', None)
    configurations = {}
    for stage in ('grouping', 'analysis', 'outlook'):
        provider, system, rules, fingerprint = await configuration(session, settings, stage)
        configurations[stage] = {'provider_id': provider.id, 'system': system, 'rules': rules,
            'provider': {key: getattr(provider, key) for key in ('model', 'protocol', 'base_url', 'max_tokens', 'context_window', 'reasoning_effort')}}
    return {'settings': value, 'configurations': configurations}


async def frozen_configuration(session, frozen, stage):
    from alphapulse.models import AiProvider
    item = frozen['configurations'][stage]
    provider = await session.get(AiProvider, item['provider_id'])
    if provider is None: raise ValueError('重整理使用的接入配置已删除')
    session.expunge(provider)
    for key, value in item['provider'].items(): setattr(provider, key, value)
    return provider, item['system'], item['rules']


async def process_job(item_index=None):
    from alphapulse.news_risk import worker
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled: return
        if item_index is None and await session.scalar(select(ArticleAnalysis.id).where(ArticleAnalysis.state == 'pending').limit(1)): return
        if item_index is None and await session.scalar(select(NewsSummary.id).where(NewsSummary.state.in_(['pending', 'processing'])).limit(1)): return
        job = await session.scalar(select(ReprocessJob).where(ReprocessJob.state.in_(['pending', 'running'])).order_by(ReprocessJob.id).limit(1).with_for_update())
        if job is None: return
        if job.position >= len(job.items):
            if await session.scalar(select(NewsSummary.id).where(NewsSummary.state.in_(['pending', 'processing'])).limit(1)): return
            from alphapulse.models.risk import NewsEvent
            if await session.scalar(select(NewsEvent.id).where(NewsEvent.state.in_(['pending','processing'])).limit(1)): return
            from alphapulse.models.risk import RiskSnapshot
            from alphapulse.news_risk import briefing
            from alphapulse.news_risk.events import reading_inputs
            from datetime import UTC, datetime, timedelta
            failed_packages = (await session.scalars(select(NewsSummary).where(
                NewsSummary.review_config == job.settings, NewsSummary.state == 'failed'))).all()
            failed_events = (await session.scalars(select(NewsEvent).where(
                NewsEvent.review_config == job.settings, NewsEvent.state == 'failed'))).all()
            failures = [{'stage': 'analysis', 'id': p.id, 'error': (p.error or '包研判失败')[:1000]} for p in failed_packages]
            failures += [{'stage': 'events', 'id': e.id, 'error': (e.error or '事件时间线失败')[:1000]} for e in failed_events]
            if not failures and not job.failures:
                inputs = await reading_inputs(session, datetime.now(UTC)-timedelta(hours=72))
                key = briefing.fingerprint(briefing.semantic_packages(inputs), job.settings['configurations']['outlook']['rules'])
                snapshot = await session.scalar(select(RiskSnapshot).where(
                    RiskSnapshot.quotes['job_id'].as_integer() == job.id,
                    RiskSnapshot.quotes['fingerprint'].as_string() == key).order_by(RiskSnapshot.id.desc()).limit(1))
                if snapshot is None or snapshot.state == 'processing' or snapshot.quotes.get('retry_requested'): return
                if snapshot.state == 'failed':
                    failures.append({'stage': 'outlook', 'id': snapshot.id, 'error': (snapshot.error or '阅读编排失败')[:1000]})
            job.failures = [*job.failures, *failures]
            job.state = 'failed' if job.failures else 'done'; await session.commit(); return
        index = job.position if item_index is None else item_index
        if index < 0: return
        item = job.items[index]
        if item.get('state') not in (None, 'pending'): return
        job.items = [{**entry, 'state': 'processing'} if n == index else entry for n, entry in enumerate(job.items)]
        job_id = job.id
        durable = await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==job.id,ReprocessItem.news_id==item['id']))
        if durable:
            durable.state='processing'
            from alphapulse.news_risk.progress import step
            step('历史新闻分包',stage='backfill',object_id=durable.id)
        try:
            for stage in ('grouping', 'analysis', 'outlook'):
                await frozen_configuration(session, job.settings, stage)
            news = await session.get(NewsItem, item['id'])
            if news is None or news.content_hash != item['hash']:
                raise ValueError('原文已修订，拒绝发布过期重整理结果')
            row = await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id == news.id, ArticleAnalysis.content_hash == news.content_hash))
            if row is None:
                row = ArticleAnalysis(news_id=news.id, content_hash=news.content_hash, state='reprocess', input={
                    'title': news.title, 'summary': news.summary, 'content': news.content_text, 'published_at': news.published_at.isoformat(),
                    'progress_at': max(news.published_at, news.source_updated_at or news.published_at).isoformat(), 'url': news.url,
                    'source': news.source_code, 'revision_no': news.revision_no})
                session.add(row); await session.flush()
            row_id = row.id; job_id = job.id; job.state = 'running'; await session.commit()
            outcome = await worker.analyze(row_id, job.settings)
            if outcome == 'deferred':
                job = await session.get(ReprocessJob, job_id, populate_existing=True, with_for_update=True)
                job.items = [{**entry, 'state': 'pending'} if n == index else entry for n, entry in enumerate(job.items)]
                await session.execute(update(ReprocessItem).where(ReprocessItem.job_id==job_id,ReprocessItem.news_id==item['id']).values(state='pending'))
                await session.commit(); return
            row = await session.get(ArticleAnalysis, row_id, populate_existing=True)
            if outcome != 'done' or row.state != 'done' or row.error: raise ValueError(row.error or '原文已变化')
            terminal = 'done'
        except Exception as error:
            terminal = 'failed'
            await session.rollback()
            job = await session.get(ReprocessJob, job_id, populate_existing=True, with_for_update=True)
            job.failures = [*job.failures, {'stage': 'grouping', 'id': item['id'], 'error': str(error)[:1000]}]
            await session.flush()
        job = await session.get(ReprocessJob, job_id, populate_existing=True, with_for_update=True)
        job.items = [{**entry, 'state': terminal} if n == index else entry for n, entry in enumerate(job.items)]
        job.position = sum(entry.get('state') in ('done', 'failed') for entry in job.items)
        await session.execute(update(ReprocessItem).where(ReprocessItem.job_id==job_id,ReprocessItem.news_id==item['id']).values(state=terminal))
        await session.commit()


async def start_job(session, start, end):
    # Serialize starts even when there is no existing row to lock.
    await session.execute(text('SELECT pg_advisory_xact_lock(20261101, 2)'))
    if await session.scalar(select(ReprocessJob.id).where(ReprocessJob.state.in_(['pending', 'running'])).limit(1)):
        raise ValueError('已有重整理任务')
    settings = await session.get(RiskSettings, 1)
    if settings is None: raise ValueError('请先配置新闻AI')
    counts, items = await preview(session, start, end)
    if not items: raise ValueError('所选范围没有新闻')
    job = ReprocessJob(state='pending', settings=await capture(session, settings), items=[{**i, 'state': 'pending'} for i in items], position=0, failures=[])
    session.add(job); await session.flush()
    job.settings = {**job.settings, 'job_id': job.id}
    await session.commit()
    return {'id': job.id, **counts}
