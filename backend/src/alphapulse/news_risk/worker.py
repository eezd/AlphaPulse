"""Independent, durable grouping and event analysis loops."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from sqlalchemy import select, update, func, exists, text, case
from sqlalchemy.dialects.postgresql import insert
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.models import NewsItem
from alphapulse.models.risk import RiskSettings, RiskSnapshot, ArticleAnalysis, NewsSummary, SummaryArticle, SummaryVersion, PackageEvidence, GroupingVersion
from alphapulse.news_risk.service import complete
from alphapulse.news_risk.pipeline import configuration, Grouping, summarize_articles, validate_evidence, Evidence, grouping_payload, pack_candidates
from alphapulse.news_risk.research import Research
from alphapulse.news_risk import briefing

logger = logging.getLogger(__name__)
analysis_lock = asyncio.Lock()
grouping_lock = asyncio.Lock()


def eligible(start):
    return (func.greatest(NewsItem.published_at, NewsItem.source_updated_at) >= start) & (NewsItem.published_at <= datetime.now(UTC))


def schedule(event, now, urgent):
    event.revision += 1
    event.changed_at = now
    event.dirty_since = event.dirty_since or now
    event.due_at = now if urgent or event.result is None else min(now+timedelta(seconds=60), event.dirty_since+timedelta(seconds=180))
    event.state = 'pending'
    event.error = None


async def enqueue(session, settings):
    rows = (await session.scalars(select(NewsItem).where(eligible(settings.start_at),
        ~exists(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id == NewsItem.id,
            ArticleAnalysis.content_hash == NewsItem.content_hash))).order_by(NewsItem.published_at.desc()).limit(200))).all()
    for news in rows:
        await session.execute(insert(ArticleAnalysis).values(news_id=news.id, content_hash=news.content_hash,
            state='pending', input={'title': news.title, 'summary': news.summary, 'content': news.content_text,
            'published_at': news.published_at.isoformat(), 'progress_at': max(news.published_at, news.source_updated_at or news.published_at).isoformat(),
            'source_tags': news.tags, 'url': news.url, 'source': news.source_code, 'revision_no': news.revision_no}).on_conflict_do_nothing())
    await session.commit()


async def analyze(target_id=None, frozen=None):
    async with grouping_lock:
        return await _analyze(target_id, frozen)


async def _analyze(target_id=None, frozen=None):
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled or not settings.start_at:
            return 'deferred'
        if target_id is None: await enqueue(session, settings)
        row = await session.scalar(select(ArticleAnalysis).join(NewsItem).where(
            ArticleAnalysis.id == target_id if target_id else ArticleAnalysis.state == 'pending', ArticleAnalysis.content_hash == NewsItem.content_hash
        ).order_by(NewsItem.published_at.desc()).limit(1))
        if row is None:
            return 'missing'
        if frozen:
            from alphapulse.news_risk.reprocess import frozen_configuration
            provider, system, rules = await frozen_configuration(session, frozen, 'grouping')
        else:
            provider, system, rules, _ = await configuration(session, settings, 'grouping')
        tag_score = sum((case((NewsSummary.tags.contains([tag.strip().casefold()]), 1), else_=0) for tag in row.input.get('source_tags', [])[:8] if isinstance(tag, str)), 0)
        candidates = (await session.scalars(select(NewsSummary).where(NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn').order_by(
            (func.greatest(func.similarity(NewsSummary.title, row.input['title']), func.similarity(NewsSummary.fact, row.input['title'])) + tag_score).desc(), NewsSummary.updated_at.desc()).limit(80))).all()
        candidate_inputs = [{'id': e.id, 'title': e.title, 'published_at': e.published_at.isoformat(), 'entities': e.entities, 'tags': e.tags, 'fact': e.fact, 'content_kind': e.content_kind} for e in candidates]
        had_success = row.result is not None
        row.state = 'processing'; row.started_at = datetime.now(UTC); row.rules = rules; row.model = provider.model
        await session.commit()
        row_id = row.id
        row_input = row.input
        try:
            payload, usages = await grouping_payload(provider, system, {'id': row.id, **row.input}, candidate_inputs)
            for attempt in range(2):
                try:
                    result, usage = await complete(provider, system, payload, Grouping)
                    usages.append(usage)
                    for match in result.packages:
                        if match.package_id is not None and match.package_id not in {e['id'] for e in payload['candidates']}:
                            raise ValueError('模型引用未知候选包')
                        validate_evidence([Evidence(article_id=row.id, quote=match.evidence)], {row.id: row.input})
                    break
                except ValueError as error:
                    if attempt:
                        raise
                    payload = pack_candidates(provider, system, {**payload, 'correction': str(error)}, payload['candidates'])
            # Check the article again before changing event membership.
            current = await session.get(NewsItem, row.news_id, populate_existing=True)
            if current.content_hash != row.content_hash:
                row.state = 'superseded'; await session.commit(); return 'superseded'
            now = datetime.now(UTC)
            old_ids = select(SummaryArticle.summary_id).join(ArticleAnalysis).where(ArticleAnalysis.news_id == row.news_id)
            old_events = (await session.scalars(select(NewsSummary).where(NewsSummary.id.in_(old_ids))
                .with_for_update().execution_options(populate_existing=True))).all()
            touched = {e.id: e for e in old_events}
            previous_ids = set(touched)
            await session.execute(update(SummaryArticle).where(SummaryArticle.analysis_id == row.id).values(active=False))
            groups = {}
            changed = set()
            for match in result.packages:
                event = await session.get(NewsSummary, match.package_id, with_for_update=True, populate_existing=True) if match.package_id else groups.get(match.group_key)
                if event is not None and (event.replacement_ids or event.state == 'withdrawn'):
                    raise ValueError('候选包已被纠错或撤回，请重试初筛')
                progress = datetime.fromisoformat(row.input['progress_at'])
                published = datetime.fromisoformat(row.input['published_at'])
                if event is None:
                    event = NewsSummary(title=match.title, category=match.category, entities=match.entities,
                        published_at=published, updated_at=progress, changed_at=now, due_at=now,
                        revision=0, published_revision=0, state='pending')
                    session.add(event); await session.flush(); groups[match.group_key] = event
                event.tags = sorted(set(event.tags or []) | {t.strip().casefold() for t in match.tags if t.strip()})
                if match.change_kind != 'repost' or not event.fact:
                    event.content_kind = match.content_kind; event.fact = match.fact
                event.published_at = min(event.published_at, published)
                if match.change_kind != 'repost' or row.input['revision_no'] > 1:
                    event.updated_at = max(event.updated_at, progress)
                link = await session.scalar(select(SummaryArticle).where(SummaryArticle.summary_id == event.id, SummaryArticle.analysis_id == row.id))
                if link is None:
                    link = SummaryArticle(summary_id=event.id, analysis_id=row.id); session.add(link)
                link.active = True; link.unit = match.model_dump()
                if match.change_kind != 'repost' or event.result is None or row.input['revision_no'] > 1:
                    changed.add(event.id)
                touched[event.id] = event
            if target_id or row.input['revision_no'] > 1:
                changed.update(previous_ids)
            for event in touched.values():
                if event.id in changed:
                    event.review_config = frozen
                    schedule(event, now, any(m.urgent for m in result.packages))
            session.add(GroupingVersion(analysis_id=row.id, input=row.input, result=result.model_dump(mode='json'), rules=rules, state='done'))
            row.result = result.model_dump(mode='json'); row.usage = {'attempts': usages}; row.state = 'done'
            row.error = None; row.finished_at = now
            await session.commit()
            return 'done'
        except Exception as error:
            await session.rollback()
            session.add(GroupingVersion(analysis_id=row_id, input=row_input, result=None, rules=rules, state='failed', error=str(error)[:2000]))
            await session.execute(update(ArticleAnalysis).where(ArticleAnalysis.id == row_id).values(
                state='done' if had_success else 'failed', error=str(error)[:2000], finished_at=datetime.now(UTC)))
            await session.commit()
            logger.exception('新闻初筛失败')
            return 'failed'


async def summarize():
    async with analysis_lock, SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled:
            return
        event = await session.scalar(select(NewsSummary).where(NewsSummary.state == 'pending',
            NewsSummary.due_at <= datetime.now(UTC)).order_by(NewsSummary.due_at, NewsSummary.id).limit(1).with_for_update())
        if event is None:
            return
        config_error = None
        if event.review_config:
            from alphapulse.news_risk.reprocess import frozen_configuration
            rules = event.review_config['configurations']['analysis']['rules']
            fingerprint = briefing.fingerprint({}, rules)
            try:
                provider, system, rules = await frozen_configuration(session, event.review_config, 'analysis')
            except ValueError as error:
                provider = None; config_error = str(error)
        else:
            provider, system, rules, fingerprint = await configuration(session, settings, 'analysis')
        rows = (await session.scalars(select(ArticleAnalysis).join(SummaryArticle).join(NewsItem).where(
            SummaryArticle.summary_id == event.id, SummaryArticle.active.is_(True), ArticleAnalysis.content_hash == NewsItem.content_hash,
            ArticleAnalysis.state == 'done').order_by(ArticleAnalysis.id))).all()
        articles = [{'id': r.id, **r.input} for r in rows]
        package = {key: getattr(event, key) for key in ('id', 'title', 'fact', 'category', 'tags', 'content_kind', 'revision')}
        package['previous'] = event.result
        if not articles:
            from alphapulse.news_risk.events import dirty_event
            await dirty_event(session, event.id, event.review_config)
            event.result = None; event.state = 'withdrawn'; await session.commit(); return
        version = await session.scalar(select(SummaryVersion).where(SummaryVersion.summary_id == event.id,
            SummaryVersion.revision == event.revision, SummaryVersion.fingerprint == fingerprint))
        if version and version.state == 'done':
            event.state = 'done'; event.error = None; event.dirty_since = None
            await session.commit()
            return
        if version is None:
            version = SummaryVersion(summary_id=event.id, revision=event.revision, fingerprint=fingerprint,
                state='processing', input={'package': package, 'articles': articles}, rules=rules, model=provider.model if provider else rules['model_config']['model'], started_at=datetime.now(UTC))
            session.add(version)
        else:
            version.state = 'processing'; version.error = None
        event.state = 'processing'
        await session.commit()
        identifier, event_id, revision = version.id, event.id, version.revision
        research = Research()
        evidence_rows = (await session.scalars(select(PackageEvidence).where(PackageEvidence.version_id == identifier).order_by(PackageEvidence.id))).all()
        research.restore([r.data for r in evidence_rows])
        try:
            if config_error: raise ValueError(config_error)
            result, usage = await summarize_articles(provider, system, articles, package, research)
            event = await session.get(NewsSummary, event_id, populate_existing=True, with_for_update=True)
            if result.correction is not None:
                from alphapulse.news_risk.corrections import apply_correction
                from alphapulse.news_risk.contracts import Correction
                await apply_correction(session, Correction.model_validate(result.correction), event_id, research.read_package_ids, rules)
                version.result = result.model_dump(mode='json'); version.usage = usage; version.state = 'corrected'; version.finished_at = datetime.now(UTC)
                for record in research.records:
                    session.add(PackageEvidence(summary_id=event_id, version_id=identifier, kind=record['tool'], reference=str(record['arguments']), data=record))
                await session.commit(); return
            version.result = result.model_dump(mode='json'); version.usage = usage; version.state = 'done'; version.finished_at = datetime.now(UTC)
            if event.revision == revision and revision > event.published_revision:
                event.result = result.model_dump(mode='json')
                event.stars = result.risk_stars; event.peak_stars = max(event.peak_stars, result.risk_stars)
                event.importance_stars = result.importance_stars
                event.published_revision = revision; event.analyzed_at = version.finished_at; event.error = None
            event.state = 'done' if event.revision == revision else 'pending'
            if event.state == 'done': event.dirty_since = None
            for record in research.records:
                session.add(PackageEvidence(summary_id=event_id, version_id=identifier, kind=record['tool'], reference=str(record['arguments']), data=record))
            from alphapulse.news_risk.events import associate
            if event.revision == revision:
                await associate(session, event, result, research.read_package_ids, research.read_event_ids)
            await session.commit()
        except Exception as error:
            await session.rollback()
            for record in research.records:
                session.add(PackageEvidence(summary_id=event_id, version_id=identifier, kind=record['tool'], reference=str(record['arguments']), data=record))
            await session.execute(update(SummaryVersion).where(SummaryVersion.id == identifier).values(
                state='failed', error=str(error)[:2000], finished_at=datetime.now(UTC)))
            await session.execute(update(NewsSummary).where(NewsSummary.id == event_id).values(
                state=case((NewsSummary.revision > revision, 'pending'), else_='failed'), error=str(error)[:2000]))
            await session.commit(); logger.exception('事件总结失败')


async def outlook():
    async with analysis_lock, SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled: return
        now = datetime.now(UTC)
        latest = await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
        requested = await session.scalar(select(RiskSnapshot).where(RiskSnapshot.state == 'failed',
            RiskSnapshot.quotes['retry_requested'].as_boolean().is_(True)).order_by(RiskSnapshot.id).limit(1).with_for_update())
        from alphapulse.news_risk.events import reading_inputs
        inputs = await reading_inputs(session, now-timedelta(hours=72))
        from alphapulse.models.risk import ReprocessJob
        job = await session.scalar(select(ReprocessJob).where(ReprocessJob.state == 'running').limit(1))
        if job and job.position < len(job.items):
            if requested and requested.quotes.get('job_id') == job.id: return
            job = None
        if job:
            from alphapulse.models.risk import NewsEvent
            if await session.scalar(select(NewsSummary.id).where(NewsSummary.state.in_(['pending', 'processing'])).limit(1)): return
            if await session.scalar(select(NewsEvent.id).where(NewsEvent.state.in_(['pending', 'processing'])).limit(1)): return
            from alphapulse.news_risk.reprocess import frozen_configuration
            provider, system, rules = await frozen_configuration(session, job.settings, 'outlook')
        else:
            provider, system, rules, _ = await configuration(session, settings, 'outlook')
        semantic = briefing.semantic_packages(inputs)
        key = briefing.fingerprint(semantic, rules)
        important = {p.get('kind', 'package')+':'+str(p['id']): briefing.fingerprint(p, {}) for p in semantic if p['importance_stars'] == 5}
        if requested is None and latest and latest.quotes.get('fingerprint') == key and (job is None or latest.quotes.get('job_id') == job.id): return
        if latest and requested is None and job is None:
            urgent_change = any(latest.quotes.get('important', {}).get(identifier) != value for identifier, value in important.items())
            if urgent_change:
                if (now-latest.created_at).total_seconds() < 60: return
            else:
                dirty = latest.quotes.get('dirty_since')
                if dirty is None:
                    latest.quotes = {**latest.quotes, 'dirty_since': now.isoformat()}
                    await session.commit(); return
                if (now-datetime.fromisoformat(dirty)).total_seconds() < 900: return
        snapshot = RiskSnapshot(state='processing', model=provider.model, quotes={'fingerprint': key, 'important': important, **({'job_id': job.id} if job else {})})
        session.add(snapshot); await session.commit()
        try:
            main = [p for p in inputs if p['importance_stars'] >= 3]
            low = [p for p in inputs if p['importance_stars'] < 3]
            result = await briefing.generate(provider, system, main) if main else {'reading': [], 'synthesis': None, 'usage': []}
            # Score all low-star candidates, then select up to three distinct domains.
            expansion = await briefing.generate(provider, system, low) if low else {'reading': []}
            domains, extra = set(), []
            by_id = {(p.get('kind', 'package'), p['id']): p for p in inputs}
            for row in expansion['reading']:
                category = by_id[(row.get('kind', 'package'), row['id'])]['category']
                if category not in domains:
                    domains.add(category); extra.append(row)
                if len(extra) == 3: break
            snapshot.result = {**result, 'expansion': extra, 'inputs': inputs, 'rules': rules,
                'expansion_usage': expansion.get('usage', []), 'as_of': now.isoformat()}
            snapshot.state = 'done'
        except Exception as error:
            snapshot.state = 'failed'; snapshot.error = str(error)[:2000]
            logger.exception('阅读编排失败')
        if requested: requested.quotes = {k: v for k, v in requested.quotes.items() if k != 'retry_requested'}
        await session.commit()


async def loop(action, heartbeat):
    while True:
        try:
            await redis.set(heartbeat, datetime.now(UTC).isoformat(), ex=1800)
            await action()
        except Exception:
            logger.exception('新闻AI处理异常')
        await asyncio.sleep(10)


async def main():
    async with engine.connect() as lock:
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(20261030, 1)')):
            raise RuntimeError('新闻AI worker 已运行')
        async with SessionLocal() as session:
            await session.execute(update(ArticleAnalysis).where(ArticleAnalysis.state == 'processing').values(state='pending'))
            await session.execute(update(NewsSummary).where(NewsSummary.state == 'processing').values(state='pending'))
            await session.execute(update(SummaryVersion).where(SummaryVersion.state == 'processing').values(state='pending'))
            await session.commit()
            await session.execute(update(RiskSnapshot).where(RiskSnapshot.state == 'processing').values(state='failed', error='进程中断，请重试'))
            await session.commit()
        from alphapulse.models.risk import NewsEvent, EventVersion
        async with SessionLocal() as session:
            await session.execute(update(NewsEvent).where(NewsEvent.state == 'processing').values(state='pending'))
            await session.execute(update(EventVersion).where(EventVersion.state == 'processing').values(state='failed', error='进程中断'))
            await session.commit()
        from alphapulse.news_risk.events import organize
        from alphapulse.news_risk.reprocess import process_job
        await asyncio.gather(loop(organize, 'ap:risk:events:heartbeat'), loop(process_job, 'ap:risk:reprocess:heartbeat'), loop(analyze, 'ap:risk:heartbeat'), loop(summarize, 'ap:risk:analysis:heartbeat'),
            loop(outlook, 'ap:risk:outlook:heartbeat'))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
