"""Exercise news risk regressions against the synthetic CI database only."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from alphapulse.ai import llm
from alphapulse.api.settings import delete_provider
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem
from alphapulse.models.risk import RiskAnalysis, RiskEvent, RiskSettings, RiskSnapshot
from alphapulse.news_risk import service, worker


def label(identifier, title, stars=0, event_id=None):
    return service.Label(id=identifier, title=title, summary=title, category='security', entities=['Exchange'],
        stars=stars, direction='unknown', verification='reported', confidence='low', reasoning='Synthetic fixture',
        uncertainty='Synthetic fixture', evidence=title, event_id=event_id)


def event(at, stars):
    return RiskEvent(title='Exchange security incident', category='security', entities=['Exchange'],
        stars=stars, peak_stars=stars, result=label(1, 'Exchange security incident', stars).model_dump(),
        published_at=at, updated_at=at)


def news(external_id, at, content='Synthetic fixture'):
    return NewsItem(source_code='wscn_news', external_id='ci-risk-'+external_id, url='https://example.invalid',
        title='Exchange security incident', summary='', content_text=content, published_at=at, content_hash=external_id)


async def main():
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.username == 'ci' and url.password == 'ci-only' and url.path == '/ci'
    assert url.hostname.startswith('ap-ci-') or url.hostname in ('127.0.0.1', 'localhost')
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RiskSettings)) == 0
        provider = AiProvider(name='ci-risk', protocol='openai', base_url='https://example.invalid',
            api_key_encrypted=b'fake', api_key_hint='fake', model='shared', max_tokens=256, context_window=200000)
        session.add(provider); await session.flush()
        provider_id = provider.id
        session.add(RiskSettings(id=1, enabled=True, provider_id=provider.id, model='synthetic-news'))
        original = event(now-timedelta(hours=3), 5)
        article = news('correction', now-timedelta(minutes=1))
        session.add_all([original, article]); await session.flush()
        original_id, article_id = original.id, article.id
        session.add(RiskAnalysis(news_id=article.id, content_hash='old-content', state='done', event_id=original.id,
            input={'title': article.title, 'published_at': article.published_at.isoformat(), 'progress_at': article.published_at.isoformat()}))
        for i in range(80):
            unrelated = event(now-timedelta(minutes=i+2), 0)
            unrelated.title = f'Company {i} ordinary earnings announcement'
            unrelated.entities = [f'Company {i}']
            unrelated.result = label(1, unrelated.title).model_dump()
            session.add(unrelated)
        await session.commit()

    payloads = []
    async def stream(provider, system, messages, tools, effort):
        payload = json.loads(messages[0]['text']); payloads.append(payload)
        if 'news' in payload:
            items = [label(n['id'], n['title'], 1 if n.get('revision_no') == 2 else 0,
                original_id if n.get('revision_no') == 2 else None) for n in payload['news']]
            result = service.Labels(items=items)
        else:
            result = service.Outlook(windows=[service.Scenario(hours=h, direction='unknown', summary='Synthetic',
                conditions='Synthetic', counter_factors='Synthetic', uncertainty='Synthetic',
                event_ids=[e['id'] for e in payload['windows'][str(h)]]) for h in service.WINDOWS])
        yield llm.Completion(tool_calls=[{'name': 'submit', 'arguments': result.model_dump()}])

    class Binance:
        def __init__(self, **kwargs): self.http = type('Http', (), {'aclose': AsyncMock()})()
        async def ticker(self, *args): return {'lastPrice': '100', 'priceChangePercent': '-6', 'closeTime': 1}

    try:
        async with SessionLocal() as session:
            candidates, _ = await worker.find_candidates(session, [SimpleNamespace(
                news_id=0, input={'title': 'Exchange security update', 'summary': ''})], now)
            assert original_id in {e.id for e in candidates}
            article = await session.get(NewsItem, article_id); article.revision_no = 2
            await session.commit()
        with patch.object(llm, 'stream', stream): await worker.analyze()
        async with SessionLocal() as session:
            original = await session.get(RiskEvent, original_id)
            assert original.stars == 1 and original.peak_stars == 5
            assert original_id in {e['id'] for e in payloads[-1]['candidates']}
            assert await session.scalar(select(func.count()).select_from(RiskEvent)) == 81
            assert (await session.get(AiProvider, provider_id)).model == 'shared'
            await session.execute(delete(RiskAnalysis)); await session.execute(delete(RiskEvent))
            session.add_all([event(now-timedelta(days=3), 5) for _ in range(100)])
            fresh = event(now-timedelta(minutes=1), 4); session.add(fresh)
            await session.flush(); fresh_id = fresh.id
            await session.commit()
        with patch.object(llm, 'stream', stream), patch.object(worker, 'BinanceClient', Binance):
            await worker.summarize()
        for hours in ('24', '48'):
            assert [e['id'] for e in payloads[-1]['windows'][hours]] == [fresh_id]
        assert len(payloads[-1]['windows']['168']) == 100
        async with SessionLocal() as session:
            snapshot = await session.scalar(select(RiskSnapshot))
            snapshot.created_at = now-timedelta(minutes=11)
            provider = await session.get(AiProvider, provider_id); provider.context_window = 8192
            await session.commit()
        with patch.object(llm, 'stream', stream), patch.object(worker, 'BinanceClient', Binance):
            await worker.summarize()
        for hours in ('24', '48'):
            assert [e['id'] for e in payloads[-1]['windows'][hours]] == [fresh_id]
        assert 0 < len(payloads[-1]['windows']['168']) < 100
        async with SessionLocal() as session:
            await session.execute(delete(RiskEvent))
            provider = await session.get(AiProvider, provider_id); provider.context_window = 8192
            article = await session.get(NewsItem, article_id); article.title = 'Batch article'
            article.content_hash = 'current'; article.content_text = 'x'*1000; article.revision_no = 3
            session.add_all([news(str(i), now-timedelta(minutes=i+2), 'x'*1000) for i in range(9)])
            session.add(news('oversized', now-timedelta(seconds=1), 'x'*30000))
            await session.commit()
        with patch.object(llm, 'stream', stream):
            for _ in range(15): await worker.analyze()
        async with SessionLocal() as session:
            counts = dict((await session.execute(select(RiskAnalysis.state, func.count()).group_by(RiskAnalysis.state))).all())
            assert counts == {'done': 10, 'failed': 1}, counts
            failed = await session.scalar(select(RiskAnalysis).where(RiskAnalysis.state == 'failed'))
            assert len(failed.input['content']) == 30000 and '上下文' in failed.error
            assert all(len(p['news']) < 10 for p in payloads if 'news' in p)
            try:
                await delete_provider(provider_id, session)
            except HTTPException as error:
                assert error.status_code == 409
            else:
                raise AssertionError('Enabled risk provider deletion was accepted')
            settings = await session.get(RiskSettings, 1); settings.enabled = False
            await session.commit()
            await delete_provider(provider_id, session)
            assert (await session.get(RiskSettings, 1)).provider_id is None
        print('News risk: correction recall, independent windows, context packing, isolated oversized article and provider deletion passed')
    finally:
        async with SessionLocal() as session:
            for model in (RiskAnalysis, RiskEvent, RiskSnapshot, RiskSettings):
                await session.execute(delete(model))
            await session.execute(delete(NewsItem).where(NewsItem.external_id.startswith('ci-risk-')))
            await session.execute(delete(AiProvider).where(AiProvider.id == provider_id))
            await session.commit()
        await engine.dispose()


asyncio.run(main())
