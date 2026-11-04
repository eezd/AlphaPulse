"""Exercise event-scoped news research using only the disposable CI database."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, func, select
from alphapulse.ai import llm
from alphapulse.ai.skills import create_node, add_version, published_skills
from alphapulse.api.settings import delete_provider
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem, SkillNode, SkillVersion
from alphapulse.models.risk import (RiskSettings, RiskSnapshot, ArticleAnalysis, NewsSummary,
    SummaryArticle, SummaryVersion, GroupingVersion, NewsEvent, EventMember, EventVersion, EventEvidence)
from alphapulse.news_risk import pipeline, worker, briefing, event_research


async def main():
    url=urlparse(os.environ['DATABASE_URL'])
    assert url.username=='ci' and url.password=='ci-only' and url.path=='/ci'
    now=datetime.now(UTC)
    async with SessionLocal() as session:
        assert not await session.scalar(select(func.count()).select_from(RiskSettings))
        provider=AiProvider(name='ci-news',protocol='openai',base_url='https://example.invalid',api_key_encrypted=b'fake',
            api_key_hint='fake',model='shared',max_tokens=8192,context_window=200000)
        session.add(provider); await session.flush(); provider_id=provider.id
        skills={}
        for role,(name,content) in pipeline.TEMPLATES.items():
            node=await create_node(session,None,'skill',name,scope='news')
            version=await add_version(session,node,content,'user','CI'); node.published_version_id=version.id
            skills[role+'_skill_id']=node.id
        await session.flush(); assert not await published_skills(session)
        session.add(RiskSettings(id=1,enabled=True,start_at=now-timedelta(minutes=2),provider_id=provider_id,
            second_provider_id=provider_id,third_provider_id=provider_id,model='luna',second_model='reviewer',third_model='senior',
            reasoning_effort='low',second_reasoning_effort='high',third_reasoning_effort='xhigh',**skills))
        for key,at in [('old',now-timedelta(days=1)),('first',now-timedelta(minutes=1)),('repost',now-timedelta(seconds=30))]:
            session.add(NewsItem(source_code='wscn_news',external_id='ci-news-'+key,url='https://example.invalid/'+key,
                title='' if key=='first' else 'Exchange reports incident',summary='',content_text='original',content_hash=key,published_at=at))
        await session.commit()
    calls=[]; broken=False; change=False
    async def stream(provider,system,messages,tools,effort):
        nonlocal change
        assert effort=={'luna':'low','reviewer':'high','senior':'xhigh'}[provider.model]
        data=json.loads(messages[0]['text']); calls.append((provider.model,data))
        if 'article' in data:
            candidate=next(iter(data['candidates']),None)
            target={'event_id':candidate['main_event_id'] if candidate else None,'group_key':None if candidate else 'incident',
                'title':'Exchange incident','fact':'Concrete incident','reason':'Same concrete occurrence'}
            result=pipeline.Grouping(reason='Same fact',packages=[dict(event=target,
                package_id=candidate['id'] if candidate else None,group_key=None if candidate else 'fact',
                title='Exchange reports incident',category='安全',entities=['Exchange'],fact='Concrete incident',
                evidence=data['article']['title'] or data['article']['content'],urgent=True,tags=['exchange'],content_kind='report',
                change_kind='repost' if candidate and data['article']['revision_no']==1 else 'correction' if candidate else 'new_fact')])
        elif data.get('task')=='event_research':
            assert provider.model=='reviewer'
            if broken: raise ValueError('Synthetic research failure')
            if change:
                change=False
                async with SessionLocal() as session:
                    event=await session.get(NewsEvent,data['id'])
                    event_research.schedule(event,datetime.now(UTC),True); await session.commit()
            refs=[dict(article_id=a['id'],quote=a['title'] or a['content']) for a in data['articles']]
            result=event_research.EventResult(id=data['id'],title='Exchange reports incident',summary='Event summary',category='安全',entities=['Exchange'],
                risk_stars=4,importance_stars=4,direction='unknown',verification='reported',confidence='low',reasoning='Synthetic',uncertainty='Unknown',
                facts='Original facts',timeline='Incident',asset_impacts='Unknown',opinions='Wait',counter_evidence='Unknown',watchpoints='Response',
                claims=[dict(statement='Incident',verdict='supported',evidence_refs=refs)],evidence_refs=refs,
                timeline_points=[dict(package_id=p['id'],package_revision=p['revision'],occurred_at=None,time_precision='unknown',
                    time_text='',reported_at=None,description=p['fact'],verification='partial',
                    evidence_refs=[r for r in refs if r['article_id'] in p['article_ids']]) for p in data['packages']])
        elif data.get('task')=='ranking':
            result=briefing.Ranked(reading=[dict(kind=p['kind'],id=p['id'],revision=p['revision'],priority=90,reason='Synthetic') for p in data['packages']],notes='Notes')
        else:
            trend=briefing.Trend(summary='Unknown',conditions='Unknown',counter_evidence='Unknown',watchpoints='Unknown')
            result=briefing.Synthesis(summary='Synthetic',market='Unknown',short_term=trend,medium_term=trend,references=[])
        yield llm.Completion(tool_calls=[{'name':'submit','arguments':result.model_dump(mode='json')}],usage={'input_tokens':10})
    try:
        with patch.object(llm,'stream',stream):
            assert await worker.analyze()=='done'
            async with SessionLocal() as session:
                event=await session.scalar(select(NewsEvent)); event_id=event.id; revision=event.revision
            assert await worker.analyze()=='done'
            async with SessionLocal() as session:
                event=await session.get(NewsEvent,event_id)
                assert event.revision==revision, 'Pure repost triggered event research'
                package=await session.scalar(select(NewsSummary)); package_id=package.id
                assert package.state=='done' and package.result is None
                assert await session.scalar(select(func.count()).select_from(SummaryVersion))==0
                assert await session.scalar(select(func.count()).select_from(SummaryArticle))==2
            await event_research.review(event_id)
            async with SessionLocal() as session:
                event=await session.get(NewsEvent,event_id)
                assert event.state=='done' and event.result['risk_stars']==4
                version=await session.scalar(select(EventVersion))
                assert len(version.input['articles'])==2
                news=await session.scalar(select(NewsItem).where(NewsItem.external_id=='ci-news-first'))
                news.content_hash='revised'; news.content_text='correction'; news.revision_no=2; news.source_updated_at=datetime.now(UTC)
                event_research.schedule(event,datetime.now(UTC),True)
                await session.commit()
            before=len(calls)
            await event_research.review(event_id)
            assert len(calls)==before, 'Outdated originals reached a model'
            async with SessionLocal() as session:
                blocked=await session.get(NewsEvent,event_id)
                assert blocked.state=='failed' and '原文已修订' in blocked.error
            assert await worker.analyze()=='done'
            await event_research.review(event_id)
            async with SessionLocal() as session:
                event=await session.get(NewsEvent,event_id)
                version=await session.scalar(select(EventVersion).order_by(EventVersion.id.desc()).limit(1))
                assert len(version.input['articles'])==2 and any(a['content']=='correction' for a in version.input['articles'])
                event_research.schedule(event,datetime.now(UTC),True); await session.commit()
                published=event.published_revision
            change=True
            await event_research.review(event_id)
            async with SessionLocal() as session:
                event=await session.get(NewsEvent,event_id)
                assert event.state=='pending' and event.published_revision==published
                assert (await session.scalar(select(EventVersion).order_by(EventVersion.id.desc()).limit(1))).state=='superseded'
            broken=True
            await event_research.review(event_id)
            async with SessionLocal() as session:
                event=await session.get(NewsEvent,event_id)
                assert event.state=='failed' and event.result['summary']=='Event summary'
                event.state='pending'; await session.commit()
            broken=False
            await event_research.review(event_id)
            await worker.outlook()
            async with SessionLocal() as session:
                snapshot=await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
                assert snapshot.state=='done' and [p['kind'] for p in snapshot.result['inputs']]==['event']
                assert (await session.get(AiProvider,provider_id)).model=='shared'
            from alphapulse.api import risk as api
            app=FastAPI(); app.include_router(api.router); app.dependency_overrides[api.require_admin]=lambda:None
            with patch.object(api.redis,'get',AsyncMock(return_value=None)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
                    for path in ('/api/risk/settings','/api/risk/overview','/api/risk/events',f'/api/risk/events/{event_id}',
                                 '/api/risk/packages',f'/api/risk/packages/{package_id}'):
                        response=await client.get(path); assert response.status_code==200,(path,response.text)
                    detail=(await client.get(f'/api/risk/events/{event_id}')).json()['data']
                    assert len(detail['packages'])==1 and detail['versions']
                    facts=(await client.get(f'/api/risk/packages/{package_id}')).json()['data']['event']
                    assert facts['article_count']==2 and 'risk_stars' not in facts and 'result' not in facts
                    assert (await client.get('/api/risk/events',params={'hours':25})).status_code==422
                    assert (await client.get('/api/risk/summaries')).status_code==404
            async with SessionLocal() as session:
                try: await delete_provider(provider_id,session)
                except HTTPException as error: assert error.status_code==409
                else: raise AssertionError('Enabled provider deleted')
                settings=await session.get(RiskSettings,1); settings.enabled=False; await session.commit()
                await delete_provider(provider_id,session)
        assert len([c for c in calls if c[0]=='luna'])==3
        assert any(not data['article']['title'] and data['article']['content'] for model,data in calls if model=='luna')
        print('Event research: grouping with untitled originals, repost suppression, originals, revision guards, retained success, retries, event-only outlook and APIs passed')
    finally:
        async with SessionLocal() as session:
            for model in (EventEvidence,EventVersion,EventMember,NewsEvent,GroupingVersion,SummaryArticle,NewsSummary,ArticleAnalysis,RiskSnapshot,RiskSettings):
                await session.execute(delete(model))
            await session.execute(delete(NewsItem).where(NewsItem.external_id.startswith('ci-news-')))
            await session.execute(delete(AiProvider).where(AiProvider.id==provider_id))
            await session.execute(SkillNode.__table__.update().where(SkillNode.id.in_(skills.values())).values(published_version_id=None))
            await session.execute(delete(SkillVersion).where(SkillVersion.skill_id.in_(skills.values())))
            await session.execute(delete(SkillNode).where(SkillNode.id.in_(skills.values())))
            await session.commit()
        await engine.dispose()


asyncio.run(main())
