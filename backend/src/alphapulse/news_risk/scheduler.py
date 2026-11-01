"""Bounded, completion-driven dispatch for every background news AI stage."""
import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from sqlalchemy import select, text, func
from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import redis
from alphapulse.models.risk import RiskSettings, NewsSummary, NewsEvent, ArticleAnalysis, ReprocessJob, ReprocessItem

logger = logging.getLogger(__name__)
GATE = 20261102


class Pool:
    def __init__(self):
        self.tasks = {}

    def start(self, key, action):
        from alphapulse.news_risk.progress import execute
        self.tasks[asyncio.create_task(execute(key, action))] = key

    async def reap(self, timeout=1):
        if not self.tasks:
            await asyncio.sleep(timeout)
            return
        done, _ = await asyncio.wait(self.tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            del self.tasks[task]
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception('新闻AI任务异常')

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def candidate(active, cursor, allow_outlook=True):
    from alphapulse.news_risk import worker, events, reprocess
    from alphapulse.models import NewsItem
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled or not settings.start_at:
            return None
        await worker.enqueue(session, settings)
        history_news = []
        for kind, identity in active:
            if kind == 'backfill':
                item = await session.get(ReprocessItem, identity)
                history_news.append(item.news_id)
            if kind == 'history' and identity[1] >= 0:
                job = await session.get(ReprocessJob, identity[0])
                history_news.append(job.items[identity[1]]['id'])
        stages = ('grouping', 'analysis', 'events', 'outlook')
        for historical in (False, True):
            for offset in range(len(stages)):
                stage = stages[(cursor + offset) % len(stages)]
                if historical and stage in ('grouping', 'outlook'): continue
                busy = [key[1] for key in active if key[0] == stage]
                if stage == 'outlook':
                    if not busy and allow_outlook:
                        try:
                            ready = await worker.outlook(prepare_only=True)
                        except ValueError:
                            logger.exception('阅读编排配置检查失败')
                            ready = False
                        if ready: return (stage, 0), worker.outlook, (cursor + offset + 1) % 4
                    continue
                model = {'grouping': ArticleAnalysis, 'analysis': NewsSummary, 'events': NewsEvent}[stage]
                query = select(model.id).where(model.state == 'pending', model.id.not_in(busy))
                if stage == 'grouping':
                    query = query.join(NewsItem).where(model.content_hash == NewsItem.content_hash, NewsItem.id.not_in(history_news)).order_by(NewsItem.published_at.desc())
                else:
                    realtime = model.review_config.is_(None) | (func.jsonb_typeof(model.review_config) == 'null')
                    query = query.where(~realtime if historical else realtime, model.due_at <= datetime.now(UTC)).order_by(model.due_at, model.id)
                    if stage == 'events':
                        from alphapulse.models.risk import EventMember
                        blocked = select(EventMember.event_id).join(NewsSummary, NewsSummary.id == EventMember.package_id).where(
                            NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn',
                            (NewsSummary.state != 'done') | NewsSummary.result.is_(None))
                        query = query.where(model.id.not_in(blocked))
                identifier = await session.scalar(query.limit(1))
                if identifier is not None:
                    action = {'grouping': worker.analyze, 'analysis': worker.summarize, 'events': events.organize}[stage]
                    return (stage, identifier), lambda action=action, identifier=identifier: action(identifier), (cursor + offset + 1) % 4
        job = await session.scalar(select(ReprocessJob).where(ReprocessJob.state.in_(['pending', 'running'])).order_by(ReprocessJob.id).limit(1))
        if job and job.settings.get('request'):
            from alphapulse.news_risk.backfill import process
            busy = [k[1] for k in active if k[0]=='backfill']
            candidates = (await session.scalars(select(ReprocessItem).where(ReprocessItem.job_id==job.id,
                ReprocessItem.state=='pending',ReprocessItem.id.not_in(busy),ReprocessItem.news_id.not_in(history_news))
                .order_by(ReprocessItem.id).limit(len(active)+1))).all()
            for item in candidates:
                article_id = await session.scalar(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id==item.news_id,
                    ArticleAnalysis.content_hash==item.content_hash))
                if ('grouping',article_id) not in active:
                    return ('backfill',item.id), lambda identifier=item.id: process(identifier), cursor
            return None
        if job:
            for index, item in enumerate(job.items):
                key = ('history', (job.id, index))
                if item.get('state', 'pending') == 'pending' and key not in active and item['id'] not in history_news:
                    # Real-time and historical grouping share the article identity.
                    row_id = await session.scalar(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id == item['id'], ArticleAnalysis.content_hash == item['hash']))
                    if row_id is not None and ('grouping', row_id) in active:
                        continue
                    return key, lambda index=index: reprocess.process_job(index), cursor
            if not any(k[0] == 'history' for k in active):
                await reprocess.process_job(-1)
    return None


async def run(lock):
    pool = Pool()
    cursor = 0
    gate = False
    next_outlook = 0.0
    # Interrupted historical items are reclaimed independently of completion order.
    async with SessionLocal() as session:
        jobs = (await session.scalars(select(ReprocessJob).where(ReprocessJob.state.in_(['pending', 'running'])))).all()
        from sqlalchemy import update
        interrupted = (await session.scalars(select(ReprocessItem).where(ReprocessItem.state=='processing'))).all()
        for item in interrupted:
            await session.execute(update(ArticleAnalysis).where(ArticleAnalysis.news_id==item.news_id, ArticleAnalysis.content_hash==item.content_hash, ArticleAnalysis.state=='pending').values(state='reprocess'))
        await session.execute(update(ReprocessItem).where(ReprocessItem.state=='processing').values(state='pending'))
        for job in jobs:
            for item in job.items:
                if item['state'] == 'processing':
                    from sqlalchemy import update
                    await session.execute(update(ArticleAnalysis).where(ArticleAnalysis.news_id == item['id'],
                        ArticleAnalysis.content_hash == item['hash'], ArticleAnalysis.state == 'pending').values(state='reprocess'))
            job.items = [{**i, 'state': 'pending' if i.get('state') == 'processing' else i['state']} for i in job.items]
        await session.commit()
    try:
        while True:
            if not pool.tasks and gate:
                await lock.execute(text('SELECT pg_advisory_unlock_shared(:gate, 1)'), {'gate': GATE})
                await lock.commit(); gate = False
            async with SessionLocal() as session:
                settings = await session.get(RiskSettings, 1)
                limit = settings.max_concurrency if settings else 5
                enabled = bool(settings and settings.enabled)
            while enabled and len(pool.tasks) < limit:
                if not gate:
                    gate = bool(await lock.scalar(text('SELECT pg_try_advisory_lock_shared(:gate, 1)'), {'gate': GATE}))
                    await lock.commit()
                    if not gate:
                        break
                picked = await candidate(set(pool.tasks.values()), cursor, time.monotonic() >= next_outlook)
                if picked is None:
                    break
                key, action, cursor = picked
                pool.start(key, action)
                if key[0] == 'outlook': next_outlook = time.monotonic() + 1
                # Refresh operational settings for each admission, including pause.
                async with SessionLocal() as session:
                    settings = await session.get(RiskSettings, 1)
                    limit, enabled = settings.max_concurrency, settings.enabled
            async with SessionLocal() as session:
                from alphapulse.news_risk.backfill import reconcile
                jobs = (await session.scalars(select(ReprocessJob).where(ReprocessJob.state.in_(['pending','running'])))).all()
                for job in jobs:
                    if job.settings.get('request'): await reconcile(session,job)
            now = datetime.now(UTC).isoformat()
            counts = {stage: sum(k[0] == stage for k in pool.tasks.values()) for stage in ('grouping', 'analysis', 'events', 'outlook', 'history', 'backfill')}
            try:
                from alphapulse.news_risk.progress import active
                await redis.set('ap:risk:active', json.dumps(list(active.values())), ex=15)
                await redis.set('ap:risk:concurrency', json.dumps({'running': len(pool.tasks), 'stages': counts, 'updated_at': now}), ex=15)
                for stage in ('', 'analysis:', 'events:', 'outlook:', 'reprocess:'):
                    await redis.set('ap:risk:' + stage + 'heartbeat', now, ex=15)
            except Exception:
                logger.exception('新闻AI运行状态暂时无法更新')
            await pool.reap()
    finally:
        await pool.close()
        if gate:
            await lock.execute(text('SELECT pg_advisory_unlock_shared(:gate, 1)'), {'gate': GATE})
            await lock.commit()
