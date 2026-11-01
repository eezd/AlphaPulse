"""Exercise both news stages against the disposable CI database and fake models."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from alphapulse.ai import llm
from alphapulse.ai.skills import create_node, add_version, published_skills
from alphapulse.api.settings import delete_provider
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem, SkillNode, SkillVersion
from alphapulse.models.risk import RiskSettings, RiskSnapshot, ArticleAnalysis, NewsSummary, SummaryArticle, SummaryVersion, PackageEvidence, GroupingVersion
from alphapulse.news_risk import service, pipeline, worker, briefing


async def main():
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.username == 'ci' and url.password == 'ci-only' and url.path == '/ci'
    assert url.hostname.startswith('ap-ci-') or url.hostname in ('127.0.0.1', 'localhost')
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RiskSettings)) == 0
        provider = AiProvider(name='ci-news', protocol='openai', base_url='https://example.invalid', api_key_encrypted=b'fake',
            api_key_hint='fake', model='shared', max_tokens=2048, context_window=200000)
        session.add(provider); await session.flush(); provider_id = provider.id
        skill_ids = {}
        for role, (name, content) in pipeline.TEMPLATES.items():
            node = await create_node(session, None, 'skill', name, scope='news')
            version = await add_version(session, node, content, 'user', 'CI')
            node.published_version_id = version.id
            skill_ids[role+'_skill_id'] = node.id
        await session.flush()
        assert not await published_skills(session), 'News skills leaked into Chat or trading'
        session.add(RiskSettings(id=1, enabled=True, provider_id=provider_id, model='luna', second_provider_id=provider_id,
            second_model='reviewer', third_provider_id=provider_id, third_model='senior',
            reasoning_effort='low', second_reasoning_effort='high', third_reasoning_effort='xhigh', start_at=now-timedelta(minutes=2), **skill_ids))
        for key, at, content in [('old', now-timedelta(days=1), 'old'), ('first', now-timedelta(minutes=1), 'original'),
                                  ('duplicate', now-timedelta(seconds=30), 'repost')]:
            session.add(NewsItem(source_code='wscn_news', external_id='ci-news-'+key, url='https://example.invalid/'+key,
                title='Exchange security incident', summary='', content_text=content, content_hash=key, published_at=at))
        await session.commit()
    calls = []
    broken = False
    inject_update = False
    finish_during_grouping = False
    async def stream(provider, system, messages, tools, effort):
        nonlocal inject_update, finish_during_grouping
        assert effort == {'luna': 'low', 'reviewer': 'high', 'senior': 'xhigh'}[provider.model]
        data = json.loads(messages[0]['text']); calls.append((provider.model, data))
        if 'article' in data:
            assert provider.model == 'luna'
            if finish_during_grouping:
                finish_during_grouping = False
                await worker.summarize()
            candidates = data['candidates']
            result = pipeline.Grouping( reason='Financial event', packages=[pipeline.Match(
                package_id=candidates[0]['id'] if candidates else None, group_key=None if candidates else 'security',
                title='Exchange security incident', category='security', entities=['Exchange'], fact='incident',
                evidence=data['article']['title'], urgent=True, tags=['Exchange'], content_kind='report')])
        elif data.get('task') == 'event':
            assert provider.model == 'reviewer'
            if broken: raise ValueError('Synthetic validation failure')
            if inject_update:
                inject_update = False
                async with SessionLocal() as other:
                    changed = await other.get(NewsSummary, data['id'], with_for_update=True)
                    worker.schedule(changed, datetime.now(UTC), True)
                    await other.commit()
            correction = any(a['content'] == 'correction' for a in data['articles'])
            result = pipeline.EventResult(id=data['id'], title='Exchange security incident', summary='Correction' if correction else 'Incident',
                category='security', entities=['Exchange'], risk_stars=1 if correction else 5, importance_stars=5, direction='unknown',
                verification='refuted' if correction else 'reported', confidence='low', reasoning='Synthetic', uncertainty='Synthetic',
                timeline_points=[dict(occurred_at=None, time_precision='unknown', time_text='Unknown', reported_at=None, description='Synthetic', verification='insufficient', evidence_refs=[{'article_id':a['id'],'quote':a['title']} for a in data['articles']])], facts='Synthetic', timeline='Synthetic',
                asset_impacts='Exchange', opinions='Uncertain', counter_evidence='Unknown', watchpoints='Updates', claims=[pipeline.Claim(statement='Synthetic', verdict='insufficient', evidence_refs=[])], related_packages=[], excluded_article_ids=[],
                evidence_refs=[pipeline.Evidence(article_id=a['id'], quote=a['title']) for a in data['articles']])
        elif data.get('task') == 'ranking':
            assert provider.model == 'senior'
            result = briefing.Ranked(reading=[briefing.Reading(id=p['id'], revision=p['revision'], priority=90, reason='Synthetic') for p in data['packages']], notes='Synthetic notes')
        else:
            trend = briefing.Trend(summary='Unknown', conditions='Unknown', counter_evidence='Unknown', watchpoints='Unknown')
            result = briefing.Synthesis(summary='Synthetic', market='Unknown', short_term=trend, medium_term=trend, references=[])
        yield llm.Completion(tool_calls=[{'name': 'submit', 'arguments': result.model_dump()}], usage={'input_tokens': 10})
    try:
        with patch.object(llm, 'stream', stream):
            await worker.analyze()
            finish_during_grouping = True
            await worker.analyze()
            async with SessionLocal() as session:
                event = await session.scalar(select(NewsSummary))
                assert event.state == 'pending' and event.revision == 2 and event.published_revision == 1, 'Grouping lost an update after concurrent summary completion'
            await worker.summarize()
            async with SessionLocal() as session:
                assert await session.scalar(select(func.count()).select_from(ArticleAnalysis)) == 2
                event = await session.scalar(select(NewsSummary)); event_id = event.id
                assert event.stars == 5 and event.revision == 2
                assert await session.scalar(select(func.count()).select_from(SummaryArticle)) == 2
                article = await session.scalar(select(NewsItem).where(NewsItem.external_id == 'ci-news-first'))
                article.content_hash = 'corrected'; article.content_text = 'correction'; article.revision_no = 2
                article.source_updated_at = datetime.now(UTC)
                await session.commit()
            await worker.analyze(); await worker.summarize()
            async with SessionLocal() as session:
                event = await session.get(NewsSummary, event_id)
                assert event.stars == 1 and event.peak_stars == 5 and event.result['verification'] == 'refuted'
                version = await session.scalar(select(SummaryVersion).order_by(SummaryVersion.id.desc()).limit(1))
                assert len(version.input['articles']) == 2, 'Superseded article revision entered the current summary'
                event.revision += 1; event.state = 'pending'; event.due_at = datetime.now(UTC)
                await session.commit()
            inject_update = True
            await worker.summarize()
            async with SessionLocal() as session:
                event = await session.get(NewsSummary, event_id)
                assert event.state == 'pending' and event.revision > event.published_revision, 'Concurrent update was lost'
            broken = True
            await worker.summarize()
            async with SessionLocal() as session:
                event = await session.get(NewsSummary, event_id)
                assert event.state == 'failed' and event.stars == 1 and event.result['summary'] == 'Correction'
            broken = False
            async with SessionLocal() as session:
                failed = RiskSnapshot(state='failed', model='reviewer', quotes={}, error='Synthetic old failure',
                    created_at=datetime.now(UTC)-timedelta(minutes=20))
                session.add(failed); await session.commit(); failed_id = failed.id
            await worker.outlook()
            async with SessionLocal() as session:
                snapshot = await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
                assert snapshot.state == 'done' and snapshot.result['inputs'][0]['id'] == event_id
                assert (await session.get(AiProvider, provider_id)).model == 'shared'
            from fastapi import FastAPI
            import httpx
            from alphapulse.api import risk as risk_api, news as news_api
            app = FastAPI()
            app.include_router(risk_api.router); app.include_router(news_api.router)
            app.dependency_overrides[risk_api.require_admin] = lambda: None
            with patch.object(risk_api.redis, 'get', AsyncMock(return_value=None)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                    for path in ('/api/risk/settings', '/api/risk/overview', '/api/risk/summaries',
                                 f'/api/risk/summaries/{event_id}', '/api/news'):
                        response = await client.get(path)
                        assert response.status_code == 200, (path, response.text)
                        assert response.json()['code'] == 0
                    response = await client.get(f'/api/risk/summaries/{event_id}')
                    assert response.json()['data']['event']['article_count'] == 2
                    assert response.json()['data']['versions']
                    response = await client.get('/api/risk/summaries', params={'hours': 25})
                    assert response.status_code == 422
                    response = await client.post(f'/api/risk/tasks/{failed_id}/retry', params={'stage': 'outlook'})
                    assert response.status_code == 202, response.text
                    await worker.outlook()
                    async with SessionLocal() as session:
                        assert await session.scalar(select(func.count()).select_from(RiskSnapshot)) == 3, 'Old failed outlook retry was ignored'
                        assert not (await session.get(RiskSnapshot, failed_id)).quotes.get('retry_requested')
            async with SessionLocal() as session:
                try: await delete_provider(provider_id, session)
                except HTTPException as error: assert error.status_code == 409
                else: raise AssertionError('Enabled model provider deletion accepted')
                settings = await session.get(RiskSettings, 1); settings.enabled = False
                await session.commit(); await delete_provider(provider_id, session)
                assert settings.provider_id is None and settings.second_provider_id is None and settings.third_provider_id is None
        print('Three-stage packages: scope, cutoff, grouping, reposts, revisions, corrections, peak risk, retained success, outlook and provider isolation passed')
    finally:
        async with SessionLocal() as session:
            for model in (GroupingVersion, PackageEvidence, SummaryVersion, SummaryArticle, NewsSummary, ArticleAnalysis, RiskSnapshot, RiskSettings):
                await session.execute(delete(model))
            await session.execute(delete(NewsItem).where(NewsItem.external_id.startswith('ci-news-')))
            await session.execute(delete(AiProvider).where(AiProvider.id == provider_id))
            await session.execute(SkillNode.__table__.update().where(SkillNode.id.in_(skill_ids.values())).values(published_version_id=None))
            await session.execute(delete(SkillVersion).where(SkillVersion.skill_id.in_(skill_ids.values())))
            await session.execute(delete(SkillNode).where(SkillNode.id.in_(skill_ids.values())))
            await session.commit()
        await engine.dispose()

asyncio.run(main())
