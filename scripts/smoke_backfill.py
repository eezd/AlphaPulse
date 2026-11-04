"""Validate event completion, queue visibility and historical scheduling in CI."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from sqlalchemy import select, func, delete, update
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem, SkillNode, SkillVersion
from alphapulse.models.risk import (RiskSettings, ArticleAnalysis, NewsSummary, SummaryArticle, NewsEvent,
    EventMember, ReprocessJob, ReprocessItem, ReprocessTarget, GroupingVersion, RiskSnapshot)
from alphapulse.ai.skills import create_node, add_version
from alphapulse.news_risk import backfill, pipeline, worker, queue, scheduler, events, briefing


async def main():
    url=urlparse(os.environ['DATABASE_URL'])
    assert url.username=='ci' and url.password=='ci-only' and url.path=='/ci'
    now=datetime.now(UTC)-timedelta(hours=3); store={}; news_ids=[]; package_ids=[]; event_ids=[]; skills={}
    async def save(key,value,**_): store[key]=value
    async def remove(key): store.pop(key,None)
    fake=SimpleNamespace(set=AsyncMock(side_effect=save),get=AsyncMock(side_effect=lambda k:store.get(k)),delete=AsyncMock(side_effect=remove))
    async with SessionLocal() as session:
        assert not await session.get(RiskSettings,1)
        provider=AiProvider(name='backfill-ci',protocol='openai',base_url='https://example.invalid',api_key_encrypted=b'fake',api_key_hint='fake',model='shared',max_tokens=8192,context_window=200000)
        session.add(provider); await session.flush(); provider_id=provider.id
        for role,(name,content) in pipeline.TEMPLATES.items():
            node=await create_node(session,None,'skill',name,scope='news')
            version=await add_version(session,node,content,'user','CI'); node.published_version_id=version.id
            skills[role+'_skill_id']=node.id
        session.add(RiskSettings(id=1,enabled=True,start_at=now+timedelta(days=1),provider_id=provider_id,second_provider_id=provider_id,
            third_provider_id=provider_id,model='luna',second_model='reviewer',third_model='senior',**skills))
        for i in range(3):
            news=NewsItem(source_code='wscn_news',external_id='ci-backfill-'+str(i),url='https://example.invalid/'+str(i),
                title='Evidence '+str(i),summary='',content_text='Evidence '+str(i),content_hash='source-'+str(i),published_at=now+timedelta(minutes=i))
            session.add(news); await session.flush(); news_ids.append(news.id)
            if i==2: continue
            article=ArticleAnalysis(news_id=news.id,content_hash=news.content_hash,state='done',result={'old':True},input={
                'title':news.title,'summary':'','content':news.content_text,'url':news.url,'source':'wscn_news','revision_no':1,
                'published_at':news.published_at.isoformat(),'progress_at':news.published_at.isoformat()})
            session.add(article); await session.flush()
            package=NewsSummary(title=news.title,fact=news.title,category='科技',entities=[],tags=['science'],
                published_at=now,updated_at=now,changed_at=now,due_at=now,revision=1,state='done')
            session.add(package); await session.flush(); package_ids.append(package.id)
            session.add(SummaryArticle(summary_id=package.id,analysis_id=article.id,active=True))
            event=NewsEvent(title=news.title,fact=news.title,updated_at=now,due_at=now,revision=1,
                published_revision=1 if i==0 else 0,state='done' if i==0 else 'failed',result={'summary':'Verified'} if i==0 else None)
            session.add(event); await session.flush(); event_ids.append(event.id)
            session.add(EventMember(package_id=package.id,event_id=event.id,reason='Same occurrence'))
        await session.commit()
    calls=[]
    async def group(provider,system,payload,schema):
        calls.append(provider.model)
        index=0 if payload['article']['title']=='Evidence 0' else 1
        return schema(reason='Concrete fact',packages=[dict(event=dict(event_id=event_ids[index],group_key=None,title='Event',fact='Fact',reason='Same event'),
            package_id=package_ids[index],group_key=None,title='Evidence',fact='Evidence',evidence=payload['article']['title'],
            category='科技',entities=[],tags=['science'],urgent=False,content_kind='report',change_kind='repost')]),{}
    async def preview(modes,start=None,end=None):
        async with SessionLocal() as session:
            return await backfill.preview(session,start or now-timedelta(seconds=1),end or now+timedelta(minutes=4),modes)
    async def begin(value):
        async with SessionLocal() as session: return (await backfill.start(session,value['preview_id']))['id']
    async def process(job_id):
        async with SessionLocal() as session: ids=list(await session.scalars(select(ReprocessItem.id).where(ReprocessItem.job_id==job_id)))
        for i in ids: await backfill.process(i)
    try:
        with patch.object(backfill,'redis',fake),patch.object(queue,'redis',fake),patch.object(worker,'complete',group):
            value=await preview(['missing'])
            assert (value['total'],value['missing'],value['review'],value['articles'])==(3,2,1,2),value
            job_id=await begin(value); await process(job_id)
            assert calls==['luna']
            async with SessionLocal() as session:
                items=list(await session.scalars(select(ReprocessItem).where(ReprocessItem.job_id==job_id)))
                assert all(i.state=='done' for i in items),[(i.state,i.error) for i in items]
                targets=list(await session.scalars(select(ReprocessTarget).where(ReprocessTarget.job_id==job_id)))
                assert len(targets)==1 and targets[0].kind=='events' and targets[0].object_id==event_ids[1]
                event=await session.get(NewsEvent,event_ids[1]); event.state='done'; event.result={'summary':'Verified'}; event.published_revision=event.revision
                await session.commit(); job=await session.get(ReprocessJob,job_id); await backfill.reconcile(session,job)
                assert job.state=='done' and (await queue.job_out(session,job))['completed']==2
                for state in ('running','queued','waiting','failed','done',None):
                    listing=await queue.listing(session,state,None,None,None,1,2)
                    assert listing['total']>=len(listing['items']) and all(r['stage']!='analysis' for r in listing['items'])
                first=await queue.listing(session,'done','backfill','history',job_id,1,1)
                second=await queue.listing(session,'done','backfill','history',job_id,2,1)
                assert first['total']==2 and first['items'][0]['key']!=second['items'][0]['key']
            value=await preview(['review'],now-timedelta(seconds=1),now+timedelta(seconds=1))
            review_id=await begin(value); await process(review_id)
            assert calls==['luna','luna']
            async with SessionLocal() as session:
                assert await session.scalar(select(func.count()).select_from(GroupingVersion))==2
                event=await session.get(NewsEvent,event_ids[0]); assert event.state=='pending'
                job=await session.get(ReprocessJob,review_id); await backfill.reconcile(session,job); assert job.state=='running'
                event.state='failed'; event.error='Synthetic failure'; await session.commit()
                await backfill.reconcile(session,job); assert job.state=='failed' and job.failures[0]['stage']=='events'
                from alphapulse.api.risk import reprocess_retry
                await reprocess_retry(job.id,session)
                assert event.state=='pending' and calls==['luna','luna']
                event.state='done'; event.result={'summary':'Reviewed'}; event.published_revision=event.revision
                await session.commit(); await backfill.reconcile(session,job); assert job.state=='done'
            value=await preview(['review'],now+timedelta(minutes=2),now+timedelta(minutes=3))
            async with SessionLocal() as session:
                settings=await session.get(RiskSettings,1); settings.model='changed'; await session.commit()
                try: await backfill.start(session,value['preview_id'])
                except ValueError as error: assert '配置已变化' in str(error)
                else: raise AssertionError('Preview configuration drift accepted')
                settings.model='luna'; await session.commit()
            changed_id=await begin(value)
            async with SessionLocal() as session:
                news=await session.get(NewsItem,news_ids[2]); news.content_hash='changed'; await session.commit()
            await process(changed_id)
            async with SessionLocal() as session:
                job=await session.get(ReprocessJob,changed_id); await backfill.reconcile(session,job)
                assert job.state=='failed' and '版本变化' in job.failures[0]['error']
                event=await session.get(NewsEvent,event_ids[0]); event.state='pending'; event.published_revision=0; await session.commit()
            value=await preview(['missing'],now-timedelta(seconds=1),now+timedelta(seconds=1)); skip_id=await begin(value)
            async with SessionLocal() as session:
                settings=await session.get(RiskSettings,1); settings.enabled=False
                event=await session.get(NewsEvent,event_ids[0]); event.state='done'; event.published_revision=event.revision
                await session.commit()
            await process(skip_id)
            async with SessionLocal() as session:
                item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==skip_id)); assert item.state=='pending'
                settings=await session.get(RiskSettings,1); settings.enabled=True; await session.commit()
            await process(skip_id)
            async with SessionLocal() as session:
                item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==skip_id)); assert item.state=='skipped'
                job=await session.get(ReprocessJob,skip_id); await backfill.reconcile(session,job); assert job.state=='done'
            value=await preview(['review'],now-timedelta(seconds=1),now+timedelta(seconds=1)); cancel_id=await begin(value)
            async with SessionLocal() as session:
                item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==cancel_id)); item.state='processing'
                article=await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id==item.news_id)); article.state='pending'
                await session.commit()
            lock=SimpleNamespace(scalar=AsyncMock(return_value=True),execute=AsyncMock(),commit=AsyncMock())
            with patch.object(scheduler,'candidate',AsyncMock(side_effect=asyncio.CancelledError)):
                try: await scheduler.run(lock)
                except asyncio.CancelledError: pass
            async with SessionLocal() as session:
                item=await session.get(ReprocessItem,item.id); assert item.state=='pending'
                article=await session.get(ArticleAnalysis,article.id); assert article.state=='reprocess'
                task={'stage':'grouping','object_id':article.id,'phase':'Waiting','started_at':now.isoformat(),'updated_at':now.isoformat()}
                article.state='processing'; await session.commit(); store['ap:risk:active']=json.dumps([task])
                listing=await queue.listing(session,'running','grouping',None,None,1,20)
                assert listing['total']==1 and listing['items'][0]['object_id']==article.id
                store.pop('ap:risk:active')
                assert (await queue.listing(session,'running','grouping',None,None,1,20))['items'][0]['state']=='unknown'
                item.state='processing'; await session.commit()
                store['ap:risk:active']=json.dumps([{**task,'stage':'backfill','object_id':item.id}])
                assert (await queue.listing(session,'running','grouping',None,None,1,20))['total']==0
                assert (await queue.listing(session,'running','backfill',None,cancel_id,1,20))['total']==1
                store.pop('ap:risk:active')
                from alphapulse.api.risk import reprocess_cancel
                await reprocess_cancel(cancel_id,session)
                shared=await session.get(NewsEvent,event_ids[0]); shared.review_config=None; shared.state='pending'; shared.due_at=now
                await backfill.target(session,cancel_id,'events',shared); await session.commit()
                listing=await queue.listing(session,'queued','events',None,cancel_id,1,20)
                assert listing['total']==1 and listing['items'][0]['origin']=='realtime'
                large=ReprocessJob(id=2147483648,state='failed',items=[],position=0,failures=[],settings={})
                session.add(large); shared.review_config={'job_id':large.id}; shared.state='failed'; await session.commit()
                listing=await queue.listing(session,'failed','events','history',large.id,1,20)
                assert listing['total']==1
                assert (await queue.listing(session,'failed',None,None,9223372036854775807,1,20))['total']==0
            no_model=AsyncMock(side_effect=AssertionError('Empty windows must not call models'))
            with patch.object(events,'reading_inputs',AsyncMock(return_value=[])),patch.object(briefing,'generate',no_model):
                assert not await worker.outlook(prepare_only=True)
                assert not await worker.outlook(prepare_only=True)
            no_model.assert_not_awaited()
            assert calls==['luna','luna']
        print('Backfill: event-scoped completion, preview snapshots, recheck, retry, pause, skip, restart, cancel, queue identity and BIGINT filters passed')
    finally:
        async with SessionLocal() as session:
            for model in (ReprocessTarget,ReprocessItem,ReprocessJob,EventMember,NewsEvent,RiskSnapshot,GroupingVersion,SummaryArticle,NewsSummary,ArticleAnalysis,RiskSettings):
                await session.execute(delete(model))
            await session.execute(delete(NewsItem).where(NewsItem.id.in_(news_ids)))
            await session.execute(update(SkillNode).where(SkillNode.id.in_(skills.values())).values(published_version_id=None))
            await session.execute(delete(SkillVersion).where(SkillVersion.skill_id.in_(skills.values())))
            await session.execute(delete(SkillNode).where(SkillNode.id.in_(skills.values())))
            await session.execute(delete(AiProvider).where(AiProvider.id==provider_id)); await session.commit()
        await engine.dispose()


asyncio.run(main())
