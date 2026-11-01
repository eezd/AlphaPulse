"""Exercise event aggregation and correction transactions in the disposable CI DB."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
from sqlalchemy import select, func, delete
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem, SkillNode, SkillVersion
from alphapulse.models.risk import NewsSummary, ArticleAnalysis, SummaryArticle, RiskSettings, NewsEvent, EventMember, EventVersion, ReprocessJob, PackageCorrection, GroupingVersion, SummaryVersion, PackageEvidence, RiskSnapshot, EventLink
from alphapulse.ai.skills import create_node, add_version
from alphapulse.news_risk import pipeline, events, reprocess, worker, briefing
from alphapulse.news_risk.contracts import Correction
from alphapulse.news_risk.corrections import apply_correction


async def main():
    url=urlparse(os.environ['DATABASE_URL'])
    assert url.username=='ci' and url.password=='ci-only' and url.path=='/ci'
    now=datetime.now(UTC)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RiskSettings))==0
        provider=AiProvider(name='event-ci',protocol='openai',base_url='https://example.invalid',api_key_encrypted=b'fake',api_key_hint='fake',model='shared',max_tokens=8192,context_window=200000)
        session.add(provider); await session.flush()
        ids={}
        for role,(name,content) in pipeline.TEMPLATES.items():
            node=await create_node(session,None,'skill',name,scope='news')
            version=await add_version(session,node,content,'user','CI'); node.published_version_id=version.id
            ids[role+'_skill_id']=node.id
        settings=RiskSettings(id=1,enabled=True,start_at=now-timedelta(minutes=5),provider_id=provider.id,second_provider_id=provider.id,third_provider_id=provider.id,model='luna',second_model='reviewer',third_model='senior',**ids)
        session.add(settings)
        packages=[]; analyses=[]
        for i in (1,2):
            news=NewsItem(source_code='wscn_news',external_id='ci-event-'+str(i),url='https://example.invalid/'+str(i),title='Incident' if i==1 else 'Response',summary='',content_text='Original evidence '+str(i),content_hash=str(i),published_at=now)
            session.add(news); await session.flush()
            analysis=ArticleAnalysis(news_id=news.id,content_hash=news.content_hash,state='done',result={'previous':True},input={'title':news.title,'summary':'','content':news.content_text,'url':news.url,'source':'wscn_news','revision_no':1,'published_at':now.isoformat(),'progress_at':now.isoformat()})
            session.add(analysis); await session.flush(); analyses.append(analysis)
            result={'title':news.title,'summary':news.title,'importance_stars':4,'risk_stars':3,'category':'security','evidence_refs':[{'article_id':analysis.id,'quote':news.content_text}],'claims':[]}
            package=NewsSummary(title=news.title,fact=news.title,category='security',tags=['exchange'],entities=['Exchange'],content_kind='report',published_at=now,updated_at=now,changed_at=now,due_at=now,state='done',revision=1,published_revision=1,result=result,importance_stars=4)
            session.add(package); await session.flush(); packages.append(package)
            session.add(SummaryArticle(summary_id=package.id,analysis_id=analysis.id,active=True,unit={'fact':news.title,'evidence':news.content_text}))
        await session.flush()
        result=SimpleNamespace(related_events=[],event_proposal=pipeline.EventProposal(title='Exchange incident',package_ids=[packages[0].id],reason='Official response to the same incident'))
        await events.associate(session,packages[1],result,{packages[0].id})
        event=await session.scalar(select(NewsEvent)); event.due_at=now
        package_ids=[p.id for p in packages]; event_id=event.id
        withdrawn = NewsSummary(title='Withdrawn',fact='Withdrawn',category='security',tags=['exchange'],entities=[],
            content_kind='report',published_at=now,updated_at=now,changed_at=now,due_at=now,state='withdrawn',revision=1,published_revision=0)
        session.add(withdrawn); await session.flush()
        session.add(EventMember(package_id=withdrawn.id,event_id=event_id,reason='Previous member'))
        await session.commit()
    async def complete(provider,system,payload,schema):
        points=[]
        for p in payload['packages']:
            points.append(events.TimelinePoint(occurred_at=None,time_precision='unknown',time_text='Time unknown',reported_at=now,description=p['result']['summary'],verification='partial',package_id=p['id'],package_revision=p['revision'],evidence_refs=p['result']['evidence_refs']))
        return schema(title='Exchange incident',summary='Disclosure followed by response',category='security',importance_stars=4,risk_stars=3,uncertainty='Time unknown',opinions='Wait',counter_evidence='Unknown',watchpoints='Investigation',timeline_points=points),{}
    with patch.object(events,'complete',complete):
        await events.organize()
        await events.organize()
    async with SessionLocal() as session:
        event=await session.get(NewsEvent,event_id)
        assert event.state=='done' and len(event.result['timeline_points'])==2
        inputs=await events.reading_inputs(session,now-timedelta(hours=72))
        assert len(inputs)==1 and inputs[0]['kind']=='event'
        assert await session.scalar(select(func.count()).select_from(EventVersion))==1
        counts,_=await reprocess.preview(session,now-timedelta(seconds=1),now+timedelta(seconds=1))
        assert counts=={'articles':2,'packages':2,'events':1}
        started=await reprocess.start_job(session,now-timedelta(seconds=1),now+timedelta(seconds=1))
        job=await session.get(ReprocessJob,started['id'])
        assert len(job.items)==2
        provider=await session.get(AiProvider,provider.id); provider.model='changed'; await session.commit()
        frozen,_,_=await reprocess.frozen_configuration(session,job.settings,'grouping')
        assert frozen.model=='luna'
        job_id = job.id
        await session.commit()
    async with SessionLocal() as session:
        event = await session.get(NewsEvent, event_id)
        event.state = 'pending'; event.revision += 1; event.due_at = now
        published = event.published_revision
        await session.commit()
    async def change_during_timeline(*args):
        async with SessionLocal() as session:
            package = await session.get(NewsSummary, package_ids[0])
            worker.schedule(package, now, True)
            await session.commit()
        return await complete(*args)
    with patch.object(events, 'complete', change_during_timeline): await events.organize()
    async with SessionLocal() as session:
        event = await session.get(NewsEvent, event_id)
        assert event.state == 'pending' and event.published_revision == published
        latest = await session.scalar(select(EventVersion).order_by(EventVersion.id.desc()).limit(1))
        assert latest.state == 'superseded'
        package = await session.get(NewsSummary, package_ids[0])
        package.state = 'done'; package.published_revision = package.revision
        await session.commit()
    with patch.object(events, 'complete', complete): await events.organize()
    analyze = worker.analyze
    async def pause_before_grouping(*args):
        async with SessionLocal() as session:
            settings = await session.get(RiskSettings, 1); settings.enabled = False
            await session.commit()
        return await analyze(*args)
    with patch.object(worker, 'analyze', pause_before_grouping): await reprocess.process_job()
    async with SessionLocal() as session:
        job = await session.get(ReprocessJob, job_id)
        assert job.position == 0 and not job.failures
        assert (await session.get(ArticleAnalysis, analyses[0].id)).result == {'previous':True}
        settings = await session.get(RiskSettings, 1); settings.enabled = True
        await session.commit()
    async def fail_grouping(*args):
        from alphapulse.api.risk import reprocess_cancel
        async with SessionLocal() as other:
            job = await other.get(ReprocessJob, job_id)
            if job.state != 'cancelled': await reprocess_cancel(job_id, other)
        raise ValueError('Synthetic grouping failure during cancellation')
    with patch.object(worker, 'complete', fail_grouping): await reprocess.process_job()
    async with SessionLocal() as session:
        job = await session.get(ReprocessJob, job_id)
        assert job.state == 'cancelled' and job.position == 1 and len(job.failures) == 1
        old = await session.get(ArticleAnalysis, analyses[0].id)
        assert old.result == {'previous':True} and old.state == 'done' and old.error
        assert await session.scalar(select(func.count()).select_from(SummaryArticle).where(SummaryArticle.active.is_(True))) == 2
        assert await session.scalar(select(func.count()).select_from(GroupingVersion).where(GroupingVersion.state == 'failed')) == 1
        second = await reprocess.start_job(session, now-timedelta(seconds=1), now+timedelta(seconds=1))
        job = await session.get(ReprocessJob, second['id'])
        job.state = 'running'; job.position = len(job.items)
        package = await session.get(NewsSummary, package_ids[0])
        package.review_config = job.settings; package.state = 'failed'; package.error = 'Synthetic review failure'
        await session.commit()
    await reprocess.process_job()
    async with SessionLocal() as session:
        job = await session.get(ReprocessJob, second['id'])
        assert job.state == 'failed' and job.failures == [{'stage':'analysis','id':package_ids[0],'error':'Synthetic review failure'}]
        from alphapulse.api.risk import reprocess_retry
        await reprocess_retry(job.id, session)
        assert job.position == len(job.items) and job.state == 'running'
        assert (await session.get(NewsSummary, package_ids[1])).state == 'done'
        assert (await session.get(NewsSummary, package_ids[0])).state == 'pending'
        assert await session.scalar(select(func.count()).select_from(GroupingVersion)) == 1
        job.state = 'cancelled'
        package = await session.get(NewsSummary, package_ids[0])
        package.state = 'done'; package.error = None; package.review_config = None
        news = NewsItem(source_code='wscn_news', external_id='ci-event-repost', url='https://example.invalid/repost',
            title='Incident', summary='', content_text='Original evidence 1', content_hash='repost', published_at=now)
        session.add(news); await session.flush()
        repost = ArticleAnalysis(news_id=news.id, content_hash=news.content_hash, state='pending', input={
            'title':news.title,'summary':'','content':news.content_text,'url':news.url,'source':'wscn_news',
            'revision_no':1,'published_at':now.isoformat(),'progress_at':now.isoformat()})
        session.add(repost); await session.commit(); analyses.append(repost)
        old_revision, old_progress = package.revision, package.updated_at
    async def group_repost(provider, system, payload, schema):
        return schema(reason='Same concrete fact', packages=[dict(package_id=package_ids[0], group_key=None,
            title='Incident', category='security', entities=['Exchange'], fact='Incident', evidence='Original evidence 1',
            urgent=False, tags=['exchange'], content_kind='report', change_kind='repost')]), {}
    with patch.object(worker, 'complete', group_repost): await worker.analyze(repost.id)
    async with SessionLocal() as session:
        package = await session.get(NewsSummary, package_ids[0])
        assert package.revision == old_revision and package.updated_at == old_progress and package.state == 'done'
        third = await reprocess.start_job(session, now-timedelta(seconds=1), now+timedelta(seconds=1))
        job = await session.get(ReprocessJob, third['id'])
        job.state = 'running'; job.position = len(job.items)
        await session.commit()
    await reprocess.process_job()
    async with SessionLocal() as session:
        assert (await session.get(ReprocessJob, third['id'])).state == 'running'
    async def senior(provider, system, payload, schema):
        assert provider.model == 'senior'
        if schema is briefing.Ranked:
            return schema(reading=[dict(kind=p['kind'], id=p['id'], revision=p['revision'], priority=80, reason='Synthetic')
                for p in payload['packages']], notes='Synthetic'), {}
        trend = briefing.Trend(summary='Unknown', conditions='Unknown', counter_evidence='Unknown', watchpoints='Updates')
        return schema(summary='Synthetic', market='Unknown', short_term=trend, medium_term=trend, references=[]), {}
    async def failed_senior(*args): raise ValueError('Synthetic outlook failure')
    with patch.object(briefing, 'complete', failed_senior): await worker.outlook()
    await reprocess.process_job()
    async with SessionLocal() as session:
        job = await session.get(ReprocessJob, third['id'])
        assert job.state == 'failed' and len(job.failures) == 1 and job.failures[0]['stage'] == 'outlook'
        groups_before = await session.scalar(select(func.count()).select_from(GroupingVersion))
        await reprocess_retry(job.id, session)
        assert job.position == len(job.items) == 3
    with patch.object(briefing, 'complete', senior): await worker.outlook()
    await reprocess.process_job()
    async with SessionLocal() as session:
        assert (await session.get(ReprocessJob, third['id'])).state == 'done'
        assert await session.scalar(select(func.count()).select_from(GroupingVersion)) == groups_before
        packages=[await session.get(NewsSummary,i) for i in package_ids]
        proposal=Correction(packages=[{'id':p.id,'revision':p.revision} for p in packages],replacements=[{'keep_id':min(package_ids),'title':'Corrected shared fact','fact':'Same fact','category':'security','tags':['exchange'],'content_kind':'report','assignments':[{'analysis_id':a.id,'evidence':a.input['content']} for a in analyses]}],reason='Synthetic duplicate correction')
        output=await apply_correction(session,proposal,package_ids[0],{package_ids[1]},{'test':True})
        await session.commit()
        assert output==[min(package_ids)]
        assert (await session.get(NewsSummary,max(package_ids))).replacement_ids==output
        assert await session.scalar(select(func.count()).select_from(PackageCorrection))==1
        assert await session.scalar(select(func.count()).select_from(SummaryArticle).where(SummaryArticle.active.is_(True)))==3
        assert await session.scalar(select(func.count()).select_from(NewsItem).where(NewsItem.external_id.startswith('ci-event-')))==3
    await events.organize()
    async with SessionLocal() as session:
        event=await session.get(NewsEvent,event_id); event.due_at=now; await session.commit()
    await events.organize()
    async with SessionLocal() as session:
        assert (await session.get(NewsEvent,event_id)).state=='retired'
        assert len(await events.reading_inputs(session,now-timedelta(hours=72)))==1
    async with SessionLocal() as session:
        for model in (EventLink, EventVersion, EventMember, NewsEvent, ReprocessJob, PackageCorrection,
                      GroupingVersion, PackageEvidence, SummaryVersion, SummaryArticle, NewsSummary, ArticleAnalysis, RiskSnapshot, RiskSettings):
            await session.execute(delete(model))
        await session.execute(delete(NewsItem).where(NewsItem.external_id.startswith('ci-event-')))
        await session.execute(delete(AiProvider).where(AiProvider.id == provider.id))
        await session.execute(SkillNode.__table__.update().where(SkillNode.id.in_(ids.values())).values(published_version_id=None))
        await session.execute(delete(SkillVersion).where(SkillVersion.skill_id.in_(ids.values())))
        await session.execute(delete(SkillNode).where(SkillNode.id.in_(ids.values())))
        await session.commit()
    await engine.dispose()
    print('Events: original evidence, independent packages, aggregation, timeline, preview, frozen config and atomic merge history passed')

asyncio.run(main())
