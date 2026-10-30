"""Single durable worker; never invokes trading or private chat tools."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from sqlalchemy import select, update, func, exists, or_
from sqlalchemy.dialects.postgresql import insert
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.models import NewsItem
from alphapulse.models.risk import RiskSettings, RiskAnalysis, RiskEvent, RiskSnapshot
from alphapulse.news_risk.service import START, SYSTEM, WINDOWS, Labels, Outlook, complete, fits_context, provider_for, validate_labels
from alphapulse.market.binance import BinanceClient

logger = logging.getLogger(__name__)
OUTLOOK_SYSTEM = SYSTEM + '\n综合给出三个窗口的市场判断，预测期限为未来数小时至24小时。' \
    '只能引用对应窗口中的事件 ID。必须区分行情已发生变化和预测，不能推断因果。历史补分析不是实时预测。' \
    '窗口没有事件时说明证据不足；不能把没有已识别风险解释为市场安全。'


async def enqueue(session):
    rows = (await session.scalars(select(NewsItem).where(
        NewsItem.published_at >= START, NewsItem.published_at <= datetime.now(UTC),
        ~exists(select(RiskAnalysis.id).where(RiskAnalysis.news_id == NewsItem.id,
                                             RiskAnalysis.content_hash == NewsItem.content_hash)),
    ).order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(200))).all()
    for news in rows:
        await session.execute(insert(RiskAnalysis).values(news_id=news.id, content_hash=news.content_hash, state='pending',
            input={'title': news.title, 'summary': news.summary, 'content': news.content_text,
                   'published_at': news.published_at.isoformat(), 'source': news.source_code,
                   'progress_at': max(news.published_at, news.source_updated_at or news.published_at).isoformat(),
                   'url': news.url, 'revision_no': news.revision_no}).on_conflict_do_nothing())
    await session.commit()


async def find_candidates(session, rows, now):
    linked = (await session.execute(select(RiskAnalysis.news_id, RiskAnalysis.event_id).where(
        RiskAnalysis.news_id.in_([row.news_id for row in rows]), RiskAnalysis.event_id.is_not(None),
    ).order_by(RiskAnalysis.id.desc()))).all()
    previous = {}
    for news_id, event_id in linked:
        previous.setdefault(news_id, event_id)
    relevance = func.greatest(*[func.similarity(RiskEvent.title, row.input['title'] + ' ' + row.input.get('summary', ''))
                               for row in rows])
    candidates = (await session.scalars(select(RiskEvent).where(or_(
        RiskEvent.id.in_(previous.values()), RiskEvent.updated_at >= now - timedelta(days=7),
    )).order_by(RiskEvent.id.in_(previous.values()).desc(), relevance.desc(),
               RiskEvent.stars.desc(), RiskEvent.updated_at.desc(), RiskEvent.id.desc()).limit(80))).all()
    return candidates, previous


def batch_payload(rows, candidates):
    return {'news': [{'id': r.id, **r.input} for r in rows], 'candidates': [
        {'id': e.id, 'title': e.title, 'category': e.category, 'entities': e.entities,
         'published_at': e.published_at.isoformat(), 'updated_at': e.updated_at.isoformat(),
         'summary': e.result['summary']} for e in candidates]}


def pack_batch(provider, rows, candidates, previous):
    rows = list(rows)
    candidates = list(candidates)
    while len(rows) > 1 and not fits_context(provider, SYSTEM, batch_payload(rows, candidates), Labels):
        rows.pop()
    required = {previous[row.news_id] for row in rows if row.news_id in previous}
    while not fits_context(provider, SYSTEM, batch_payload(rows, candidates), Labels):
        optional = next((e for e in reversed(candidates) if e.id not in required), None)
        if optional is None:
            break
        candidates.remove(optional)
    # A source that cannot fit alone fails explicitly; other articles stay queued.
    return rows, candidates, batch_payload(rows, candidates)


async def analyze():
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled:
            return
        provider = await provider_for(session, settings)
        await enqueue(session)
        rows = (await session.scalars(select(RiskAnalysis).join(NewsItem).where(
            RiskAnalysis.state == 'pending', RiskAnalysis.content_hash == NewsItem.content_hash,
        ).order_by(func.greatest(NewsItem.published_at, NewsItem.source_updated_at).desc(), NewsItem.id.desc()).limit(10))).all()
        if not rows:
            return
        now = datetime.now(UTC)
        candidates, previous = await find_candidates(session, rows, now)
        rows, candidates, payload = pack_batch(provider, rows, candidates, previous)
        for row in rows:
            row.state = 'processing'; row.started_at = now; row.model = provider.model
        await session.commit()
        ids = [r.id for r in rows]
        try:
            result, usage = await complete(provider, SYSTEM, payload, Labels)
            validate_labels(result, {r.id: r.input for r in rows}, {e.id for e in candidates})
            assigned = {}
            for label in sorted(result.items, key=lambda label: ids.index(label.id)):
                row = next(r for r in rows if r.id == label.id)
                row.result = label.model_dump(); row.state = 'done'; row.finished_at = datetime.now(UTC)
                # Usage belongs to one batch, recorded once rather than multiplied per article.
                row.usage = usage if row.id == ids[0] else None
                current = await session.get(NewsItem, row.news_id, populate_existing=True)
                if current.content_hash != row.content_hash:
                    continue
                published = datetime.fromisoformat(row.input['published_at'])
                progress = datetime.fromisoformat(row.input['progress_at'])
                target = assigned.get(label.same_event_as) if label.same_event_as else label.event_id
                event = await session.get(RiskEvent, target) if target else None
                if event is None:
                    event = RiskEvent(title=label.title, category=label.category, entities=label.entities,
                        stars=label.stars, peak_stars=label.stars, result=label.model_dump(),
                        published_at=published, updated_at=progress)
                    session.add(event); await session.flush()
                else:
                    event.peak_stars = max(event.peak_stars, label.stars)
                    event.published_at = min(event.published_at, published)
                    if progress >= event.updated_at:
                        event.title = label.title; event.result = label.model_dump(); event.stars = label.stars
                        event.updated_at = progress; event.entities = label.entities; event.category = label.category
                row.event_id = event.id
                assigned[row.id] = event.id
            await session.commit()
        except Exception as error:
            await session.rollback()
            await session.execute(update(RiskAnalysis).where(RiskAnalysis.id.in_(ids)).values(
                state='failed', error=str(error)[:2000], finished_at=datetime.now(UTC)))
            await session.commit()
            logger.exception('新闻风控分析失败')


async def summarize():
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled:
            return
        latest = await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
        urgent = await session.scalar(select(func.max(RiskAnalysis.finished_at)).join(RiskEvent).where(RiskEvent.stars >= 4))
        delay = 60 if urgent and latest and urgent > latest.created_at else 600
        if latest and (now - latest.created_at).total_seconds() < delay:
            return
        windows = {}
        for hours in WINDOWS:
            events = (await session.scalars(select(RiskEvent).where(
                RiskEvent.updated_at >= now - timedelta(hours=hours), RiskEvent.stars > 0,
            ).order_by(RiskEvent.stars.desc(), RiskEvent.updated_at.desc(), RiskEvent.id.desc()).limit(100))).all()
            windows[str(hours)] = [{**e.result, 'id': e.id, 'stars': e.stars, 'at': e.updated_at.isoformat()} for e in events]
        if not any(windows.values()) and not await session.scalar(select(RiskAnalysis.id).where(RiskAnalysis.state == 'done').limit(1)):
            return
        provider = await provider_for(session, settings)
        quotes = {'at': now.isoformat()}
        snapshot = RiskSnapshot(state='failed', model=provider.model, quotes=quotes)
        try:
            client = BinanceClient(wait=False)
            try:
                for symbol in ('BTCUSDT', 'ETHUSDT'):
                    ticker = await client.ticker('um', symbol)
                    quotes[symbol] = {key: ticker[key] for key in ('lastPrice', 'priceChangePercent', 'closeTime')}
            finally:
                await client.http.aclose()
            payload = {'as_of': now.isoformat(), 'quotes': quotes, 'windows': windows}
            while not fits_context(provider, OUTLOOK_SYSTEM, payload, Outlook):
                largest = max(windows.values(), key=len)
                if len(largest) <= 1:
                    break
                largest.pop()
            result, _ = await complete(provider, OUTLOOK_SYSTEM, payload, Outlook)
            if sorted(w.hours for w in result.windows) != list(WINDOWS):
                raise ValueError('综合判断窗口不完整或重复')
            for window in result.windows:
                allowed = {e['id'] for e in payload['windows'][str(window.hours)]}
                if not set(window.event_ids) <= allowed:
                    raise ValueError('综合判断引用未知事件')
            snapshot.state = 'done'; snapshot.result = result.model_dump()
        except Exception as error:
            snapshot.error = str(error)[:2000]
            logger.exception('新闻风控综合判断失败')
        snapshot.quotes = quotes
        session.add(snapshot); await session.commit()


async def main():
    # A session advisory lock prevents duplicate workers without holding business transactions open.
    async with engine.connect() as lock:
        from sqlalchemy import text
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(20261030, 1)')):
            raise RuntimeError('新闻风控 worker 已运行')
        async with SessionLocal() as session:
            await session.execute(update(RiskAnalysis).where(RiskAnalysis.state == 'processing').values(state='pending'))
            await session.commit()
        while True:
            try:
                await redis.set('ap:risk:heartbeat', datetime.now(UTC).isoformat(), ex=1800)
                await analyze(); await summarize()
            except Exception:
                logger.exception('新闻风控任务异常')
            await asyncio.sleep(10)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
