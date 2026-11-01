"""Validate backfill modes and queue queries only in a disposable CI database."""
import asyncio
import os
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from urllib.parse import urlparse
from sqlalchemy import select, delete, update, func
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem, SkillVersion, SkillNode
from alphapulse.models.risk import (RiskSettings, ArticleAnalysis, SummaryArticle, NewsSummary,
    GroupingVersion, ReprocessJob, ReprocessItem, ReprocessTarget, NewsEvent, EventMember, PackageCorrection, RiskSnapshot)
from alphapulse.ai.skills import create_node, add_version
from alphapulse.news_risk import backfill, pipeline, worker, queue, scheduler, events, briefing


async def main():
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.username == 'ci' and url.password == 'ci-only' and url.path == '/ci'
    now = datetime.now(UTC) - timedelta(hours=3)
    storage = {}
    async def save(key, value, **_): storage[key] = value
    async def remove(key): storage.pop(key, None)
    fake_redis = type('Redis', (), {'set': AsyncMock(side_effect=save), 'get': AsyncMock(side_effect=lambda k: storage.get(k)), 'delete': AsyncMock(side_effect=remove)})()
    async with SessionLocal() as session:
        assert not await session.scalar(select(func.count()).select_from(RiskSettings))
        provider = AiProvider(name='backfill-ci', protocol='openai', base_url='https://example.invalid',
            api_key_encrypted=b'fake', api_key_hint='fake', model='shared', max_tokens=8192, context_window=200000)
        session.add(provider); await session.flush()
        skills = {}
        for stage, (name, content) in pipeline.TEMPLATES.items():
            node = await create_node(session, None, 'skill', name, scope='news')
            version = await add_version(session, node, content, 'user', 'CI')
            node.published_version_id = version.id; skills[stage+'_skill_id'] = node.id
        session.add(RiskSettings(id=1, enabled=True, start_at=now+timedelta(days=1), provider_id=provider.id,
            second_provider_id=provider.id, third_provider_id=provider.id, model='luna', second_model='reviewer',
            third_model='senior', reasoning_effort='low', second_reasoning_effort='high', **skills))
        news_ids, package_ids = [], []
        for i in range(3):
            news = NewsItem(source_code='wscn_news', external_id='ci-backfill-'+str(i),
                url='https://example.invalid/'+str(i), title='Evidence '+str(i), summary='',
                content_text='Evidence '+str(i), content_hash='ci-backfill-'+str(i), published_at=now+timedelta(minutes=i))
            session.add(news); await session.flush(); news_ids.append(news.id)
            if i == 2: continue
            article = ArticleAnalysis(news_id=news.id, content_hash=news.content_hash, state='done', result={'old':True},
                input={'title':news.title,'summary':'','content':news.content_text,'url':news.url,'source':'wscn_news',
                    'revision_no':1,'published_at':news.published_at.isoformat(),'progress_at':news.published_at.isoformat()})
            session.add(article); await session.flush()
            package = NewsSummary(title=news.title, fact=news.title, category='science', entities=[], tags=[],
                published_at=now, updated_at=now, changed_at=now, due_at=now, revision=1, published_revision=1 if i==0 else 0,
                state='done' if i==0 else 'failed', result={'summary':'Original'} if i==0 else None)
            session.add(package); await session.flush(); package_ids.append(package.id)
            session.add(SummaryArticle(summary_id=package.id, analysis_id=article.id, active=True))
        await session.commit()
    grouping_calls = []
    async def group(provider, system, payload, schema):
        grouping_calls.append(provider.model)
        article = payload['article']
        return pipeline.Grouping(reason='Same concrete fact', packages=[dict(package_id=package_ids[1], group_key=None,
            title='Evidence', fact='Evidence', evidence=article['title'], category='science', entities=[], tags=['science'],
            urgent=False, change_kind='repost', content_kind='report')]), {}
    async def prepare(start, end, modes):
        async with SessionLocal() as session: return await backfill.preview(session, start, end, modes)
    async def begin(value):
        async with SessionLocal() as session: return (await backfill.start(session, value['preview_id']))['id']
    async def process_all(job_id):
        async with SessionLocal() as session:
            ids = (await session.scalars(select(ReprocessItem.id).where(ReprocessItem.job_id==job_id))).all()
        for identifier in ids: await backfill.process(identifier)
    with patch.object(backfill, 'redis', fake_redis), patch.object(queue, 'redis', fake_redis), patch.object(worker, 'complete', group):
        window = (now-timedelta(seconds=1), now+timedelta(minutes=4))
        preview = await prepare(*window, ['missing'])
        assert (preview['total'],preview['missing'],preview['review'],preview['articles']) == (3,2,1,2), preview
        job_id = await begin(preview)
        await process_all(job_id)
        assert grouping_calls == ['luna'], grouping_calls
        async with SessionLocal() as session:
            items = (await session.scalars(select(ReprocessItem).where(ReprocessItem.job_id==job_id))).all()
            assert all(i.state=='done' for i in items), [(i.state,i.error) for i in items]
            targets = (await session.scalars(select(ReprocessTarget).where(ReprocessTarget.job_id==job_id))).all()
            assert len(targets)==1 and targets[0].object_id==package_ids[1]
            package = await session.get(NewsSummary, package_ids[1]); revision = package.revision
            package.state='done'; package.result={'summary':'Verified'}; package.published_revision=revision
            # An unrelated pending package must not hold this batch open.
            other = NewsSummary(title='Unrelated',fact='Other',category='science',entities=[],published_at=now,
                updated_at=now,changed_at=now,due_at=now,state='pending')
            session.add(other); await session.commit()
            job = await session.get(ReprocessJob,job_id); await backfill.reconcile(session,job)
            assert job.state=='done', (job.state,job.failures)
            assert (await queue.job_out(session,job))['completed']==2
            for state in ('running','queued','waiting','failed','done',None):
                result = await queue.listing(session,state,None,None,None,1,2)
                assert result['total']>=len(result['items'])
            result = await queue.listing(session,'done','backfill','history',job_id,1,1)
            next_page = await queue.listing(session,'done','backfill','history',job_id,2,1)
            assert result['total']==2 and result['items'][0]['key']!=next_page['items'][0]['key']
        # Recheck must rerun grouping, preserve grouping history and refresh old/new targets.
        preview = await prepare(now-timedelta(seconds=1),now+timedelta(seconds=1),['review'])
        review_id = await begin(preview)
        await process_all(review_id)
        assert grouping_calls==['luna','luna']
        async with SessionLocal() as session:
            item = await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==review_id))
            assert item.state=='done', item.error
            assert await session.scalar(select(func.count()).select_from(GroupingVersion))==2
            targets = (await session.scalars(select(ReprocessTarget).where(ReprocessTarget.job_id==review_id))).all()
            assert {t.object_id for t in targets}==set(package_ids)
            for target in targets:
                package=await session.get(NewsSummary,target.object_id)
                package.state='done'; package.result={'summary':'Verified'}; package.published_revision=package.revision
            event=NewsEvent(title='Related event',updated_at=now,due_at=now,state='pending',revision=1)
            session.add(event); await session.flush()
            session.add(EventMember(package_id=package_ids[1],event_id=event.id,reason='Synthetic'))
            await session.commit()
            job=await session.get(ReprocessJob,review_id); await backfill.reconcile(session,job)
            assert job.state=='running'
            event.state='done'; event.published_revision=1; await session.commit()
            await backfill.reconcile(session,job); assert job.state=='done'
        # The fixed preview refuses changed settings, and a changed source fails visibly.
        preview = await prepare(now+timedelta(minutes=2),now+timedelta(minutes=3),['review'])
        async with SessionLocal() as session:
            settings=await session.get(RiskSettings,1); settings.model='changed'; await session.commit()
            try: await backfill.start(session,preview['preview_id'])
            except ValueError as error: assert '配置已变化' in str(error)
            else: raise AssertionError('Changed configuration accepted')
            settings.model='luna'; await session.commit()
        changed_id = await begin(preview)
        async with SessionLocal() as session:
            news=await session.get(NewsItem,news_ids[2]); news.content_hash='changed'; await session.commit()
        await process_all(changed_id)
        async with SessionLocal() as session:
            job=await session.get(ReprocessJob,changed_id); await backfill.reconcile(session,job)
            item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==changed_id))
            assert job.state=='failed' and '版本变化' in item.error
        assert grouping_calls==['luna','luna']
        # Missing items completed after preview are skipped, and pause/cancel stop admissions.
        async with SessionLocal() as session:
            package=await session.get(NewsSummary,package_ids[1]); package.published_revision=0; package.state='pending'
            await session.commit()
        preview=await prepare(now-timedelta(seconds=1),now+timedelta(seconds=1),['missing'])
        skip_id=await begin(preview)
        async with SessionLocal() as session:
            settings=await session.get(RiskSettings,1); settings.enabled=False
            package=await session.get(NewsSummary,package_ids[1]); package.published_revision=package.revision; package.state='done'
            await session.commit()
        await process_all(skip_id)
        async with SessionLocal() as session:
            item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==skip_id)); assert item.state=='pending'
            settings=await session.get(RiskSettings,1); settings.enabled=True; await session.commit()
        await process_all(skip_id)
        async with SessionLocal() as session:
            item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==skip_id)); assert item.state=='skipped'
            job=await session.get(ReprocessJob,skip_id); await backfill.reconcile(session,job); assert job.state=='done'
            # A failed member must fail its batch without waiting forever for the blocked event.
            job=await session.get(ReprocessJob,review_id); job.state='running'
            package=await session.get(NewsSummary,package_ids[1]); package.state='failed'; package.error='Synthetic failure'
            event=await session.scalar(select(NewsEvent)); event.state='pending'; event.revision+=1
            await session.commit()
            await backfill.reconcile(session,job)
            assert job.state=='failed' and {f['stage'] for f in job.failures}=={'analysis','events'}, job.failures
            from alphapulse.api.risk import reprocess_retry
            await reprocess_retry(job.id,session)
            assert (await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==job.id))).state=='done'
            assert (await session.get(NewsSummary,package_ids[1])).state=='pending'
            job.state='cancelled'; await session.commit()
        preview=await prepare(now-timedelta(seconds=1),now+timedelta(seconds=1),['review'])
        cancel_id=await begin(preview)
        async with SessionLocal() as session:
            item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==cancel_id)); item.state='processing'
            article=await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id==item.news_id)); article.state='pending'
            await session.commit()
        lock=SimpleNamespace(scalar=AsyncMock(return_value=True),execute=AsyncMock(),commit=AsyncMock())
        with patch.object(scheduler,'candidate',AsyncMock(side_effect=asyncio.CancelledError)):
            try: await scheduler.run(lock)
            except asyncio.CancelledError: pass
            else: raise AssertionError('Restart test did not reach admission')
        async with SessionLocal() as session:
            item=await session.scalar(select(ReprocessItem).where(ReprocessItem.job_id==cancel_id)); assert item.state=='pending'
            article=await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id==item.news_id)); assert article.state=='reprocess'
            job=await session.get(ReprocessJob,cancel_id); job.state='cancelled'; await session.commit()
        await process_all(cancel_id)
        async with SessionLocal() as session:
            job=await session.get(ReprocessJob,cancel_id); await backfill.reconcile(session,job)
            assert job.state=='cancelled'
        assert grouping_calls==['luna','luna']
        # Cancelled calls drain before another batch can review the same news.
        from alphapulse.api.risk import reprocess_cancel
        async with SessionLocal() as session:
            config=json.loads(json.dumps((await session.get(ReprocessJob,cancel_id)).settings))
            config['configurations']['grouping']['provider']['model']='luna-old'
            old=ReprocessJob(state='pending',items=[],position=0,failures=[],settings=config)
            session.add(old); await session.flush(); old.settings={**old.settings,'job_id':old.id}
            news=await session.get(NewsItem,news_ids[0])
            old_item=ReprocessItem(job_id=old.id,news_id=news.id,content_hash=news.content_hash,mode='review',state='pending')
            session.add(old_item); await session.commit()
            old_id,old_item_id=old.id,old_item.id
        entered=asyncio.Event(); release=asyncio.Event()
        async def controlled(provider,system,payload,schema):
            if provider.model=='luna-old':
                entered.set(); await release.wait()
            result,usage=await group(provider,system,payload,schema)
            result.reason=provider.model
            return result,usage
        with patch.object(worker,'complete',controlled),patch.object(worker,'outlook',AsyncMock(return_value=False)):
            old_task=asyncio.create_task(backfill.process(old_item_id))
            try:
                async with asyncio.timeout(5): await entered.wait()
                async with SessionLocal() as session:
                    await reprocess_cancel(old_id,session)
                    config=json.loads(json.dumps((await session.get(ReprocessJob,old_id)).settings))
                    config['configurations']['grouping']['provider']['model']='luna-new'
                    new=ReprocessJob(state='pending',items=[],position=0,failures=[],settings=config)
                    session.add(new); await session.flush(); new.settings={**new.settings,'job_id':new.id}
                    retried=ReprocessItem(job_id=new.id,news_id=news_ids[0],content_hash='ci-backfill-0',mode='review',state='pending')
                    other_item=ReprocessItem(job_id=new.id,news_id=news_ids[1],content_hash='ci-backfill-1',mode='review',state='pending')
                    session.add_all([retried,other_item]); await session.commit()
                    new_id,retried_id,other_item_id=new.id,retried.id,other_item.id
                    active={('analysis',i) for i in (await session.scalars(select(NewsSummary.id))).all()}
                    active.update(('events',i) for i in (await session.scalars(select(NewsEvent.id))).all())
                picked=await scheduler.candidate(active|{('backfill',old_item_id)},0)
                assert picked and picked[0]==('backfill',other_item_id), picked
                assert 'luna-new' not in grouping_calls
            finally:
                release.set(); await old_task
            picked=await scheduler.candidate(active,0)
            assert picked and picked[0]==('backfill',retried_id), picked
            await picked[1]()
            async with SessionLocal() as session:
                article=await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id==news_ids[0]))
                assert article.result['reason']=='luna-new' and article.model=='luna-new'
                new=await session.get(ReprocessJob,new_id); new.state='cancelled'; await session.commit()
        # A pending backfill must not hide real-time grouping that won admission.
        async with SessionLocal() as session:
            job=ReprocessJob(state='running',items=[],position=0,failures=[],settings=(await session.get(ReprocessJob,cancel_id)).settings)
            session.add(job); await session.flush()
            job.settings={**job.settings,'job_id':job.id}
            news=await session.get(NewsItem,news_ids[0])
            item=ReprocessItem(job_id=job.id,news_id=news.id,content_hash=news.content_hash,mode='review',state='pending')
            session.add(item)
            article=await session.scalar(select(ArticleAnalysis).where(ArticleAnalysis.news_id==news.id)); article.state='processing'
            await session.commit()
            task={'stage':'grouping','object_id':article.id,'phase':'等待模型响应',
                'started_at':now.isoformat(),'updated_at':now.isoformat()}
            storage['ap:risk:active']=json.dumps([task])
            result=await queue.listing(session,'running','grouping',None,None,1,20)
            assert [r['object_id'] for r in result['items']]==[article.id] and result['total']==1
            storage.pop('ap:risk:active')
            result=await queue.listing(session,'running','grouping',None,None,1,20)
            assert len(result['items'])==1 and result['items'][0]['state']=='unknown'
            item.state='processing'; await session.commit()
            storage['ap:risk:active']=json.dumps([{**task,'stage':'backfill','object_id':item.id}])
            assert (await queue.listing(session,'running','grouping',None,None,1,20))['total']==0
            assert (await queue.listing(session,'running','backfill',None,job.id,1,20))['total']==1
            storage.pop('ap:risk:active')
            # Batch membership follows durable targets; execution metadata remains real-time.
            shared=await session.get(NewsSummary,package_ids[1]); shared.review_config=None; shared.state='pending'; shared.due_at=now
            await backfill.target(session,job.id,'analysis',shared)
            await session.commit()
            result=await queue.listing(session,'queued','analysis',None,job.id,1,20)
            assert result['total']==1 and result['items'][0]['object_id']==shared.id
            assert result['items'][0]['origin']=='realtime' and result['items'][0]['model']=='reviewer'
            assert (await queue.listing(session,'queued','analysis','realtime',job.id,1,20))['total']==1
            assert (await queue.listing(session,'queued','analysis','history',job.id,1,20))['total']==0
            event=await session.scalar(select(NewsEvent)); event.review_config=None; event.state='pending'; event.due_at=now
            shared.state='done'; shared.published_revision=shared.revision
            await backfill.target(session,job.id,'events',event)
            await session.commit()
            result=await queue.listing(session,'queued','events',None,job.id,1,20)
            assert result['total']==1 and result['items'][0]['object_id']==event.id
            await session.execute(delete(ReprocessTarget).where(ReprocessTarget.job_id==job.id))
            await session.commit()
            # Retaining the original package ID during a split must track both branches.
            article.state='done'; item.state='done'
            package=NewsSummary(title='Two facts',fact='Two facts',category='science',entities=[],tags=['science'],
                published_at=now,updated_at=now,changed_at=now,due_at=now,state='done',revision=1,published_revision=1,
                result={'summary':'Original'},review_config=job.settings)
            session.add(package); await session.flush()
            session.add(SummaryArticle(summary_id=package.id,analysis_id=article.id,active=True))
            await session.flush()
            await backfill.target(session,job.id,'analysis',package)
            from alphapulse.news_risk.contracts import Correction
            from alphapulse.news_risk.corrections import apply_correction
            proposal=Correction(packages=[{'id':package.id,'revision':package.revision}],replacements=[
                {'keep_id':package.id,'title':'First fact','fact':'First fact','category':'science','tags':['first'],
                    'content_kind':'report','assignments':[{'analysis_id':article.id,'evidence':article.input['content']}]},
                {'keep_id':None,'title':'Second fact','fact':'Second fact','category':'science','tags':['second'],
                    'content_kind':'report','assignments':[{'analysis_id':article.id,'evidence':article.input['content']}]}],
                reason='Two distinct facts in one article')
            outputs=await apply_correction(session,proposal,package.id,set(),{'backfill-split':True})
            package.state='done'; package.published_revision=package.revision
            await session.commit()
            await backfill.reconcile(session,job)
            assert job.state=='running'
            assert set((await session.scalars(select(ReprocessTarget.object_id).where(
                ReprocessTarget.job_id==job.id,ReprocessTarget.kind=='analysis'))).all())==set(outputs)
            created=await session.get(NewsSummary,outputs[1]); created.state='failed'; created.error='Synthetic split failure'
            await session.commit(); await backfill.reconcile(session,job)
            assert job.state=='failed' and job.failures==[{'stage':'analysis','id':created.id,'error':created.error}]
            await reprocess_retry(job.id,session)
            assert (await session.get(NewsSummary,created.id)).state=='pending'
            created.state='done'; created.published_revision=created.revision; created.result={'summary':'Verified split'}
            await session.commit(); await backfill.reconcile(session,job); assert job.state=='done'
        # An empty 72h window clears old reading immediately, without models or a pool slot.
        async with SessionLocal() as session:
            previous=RiskSnapshot(state='done',model='senior',quotes={'fingerprint':'obsolete','important':{}},
                result={'reading':[{'id':package.id}],'inputs':[{'id':package.id}],'synthesis':{'summary':'Obsolete'}})
            session.add(previous); await session.commit()
            count=await session.scalar(select(func.count()).select_from(RiskSnapshot))
        no_model=AsyncMock(side_effect=AssertionError('Empty windows must not call a model'))
        with patch.object(events,'reading_inputs',AsyncMock(return_value=[])),patch.object(briefing,'generate',no_model):
            assert not await worker.outlook(prepare_only=True)
            assert not await worker.outlook(prepare_only=True)
            async with SessionLocal() as session:
                latest=await session.scalar(select(RiskSnapshot).order_by(RiskSnapshot.id.desc()).limit(1))
                assert latest.state=='done' and latest.result['inputs']==[] and latest.result['reading']==[]
                assert latest.result['expansion']==[] and latest.result['synthesis'] is None
                assert await session.scalar(select(func.count()).select_from(RiskSnapshot))==count+1
                latest.state='failed'; latest.quotes={**latest.quotes,'retry_requested':True}; await session.commit()
                failed_id=latest.id
            assert not await worker.outlook(prepare_only=True)
            async with SessionLocal() as session:
                previous=await session.get(RiskSnapshot,failed_id); assert 'retry_requested' not in previous.quotes
                assert await session.scalar(select(func.count()).select_from(RiskSnapshot))==count+2
            assert not await worker.outlook(prepare_only=True)
        no_model.assert_not_awaited()
        # JSON batch references must preserve the BIGINT identity used by job rows.
        async with SessionLocal() as session:
            original=await session.get(ReprocessJob,job_id)
            large=ReprocessJob(id=2147483648,state='failed',settings={**original.settings,'job_id':2147483648},
                items=[],position=0,failures=[])
            package=await session.get(NewsSummary,package_ids[0])
            package.review_config=large.settings; package.state='failed'
            event=NewsEvent(title='Large batch',updated_at=now,due_at=now,state='failed',review_config=large.settings)
            snapshot=RiskSnapshot(state='failed',model='senior',quotes={'job_id':large.id})
            session.add_all([large,event,snapshot]); await session.flush()
            large.failures=[{'stage':'outlook','id':snapshot.id,'error':'Synthetic large batch failure'}]
            await session.commit()
            result=await queue.listing(session,'failed',None,'history',large.id,1,20)
            assert {(r['stage'],r['object_id']) for r in result['items']}=={
                ('analysis',package.id),('events',event.id),('outlook',snapshot.id)}
            for state in ('running','queued','waiting','failed','done',None):
                result=await queue.listing(session,state,None,None,9223372036854775807,1,20)
                assert result['total']==0 and result['items']==[]
            from alphapulse.api.risk import reprocess_retry
            await reprocess_retry(large.id,session)
            assert (await session.get(RiskSnapshot,snapshot.id)).quotes['retry_requested']
    async with SessionLocal() as session:
        for model in (ReprocessTarget,ReprocessItem,ReprocessJob,EventMember,NewsEvent,PackageCorrection,RiskSnapshot,GroupingVersion,SummaryArticle,NewsSummary,ArticleAnalysis):
            await session.execute(delete(model))
        await session.execute(delete(NewsItem).where(NewsItem.id.in_(news_ids)))
        await session.execute(delete(RiskSettings))
        await session.execute(update(SkillNode).where(SkillNode.id.in_(skills.values())).values(published_version_id=None))
        await session.execute(delete(SkillVersion).where(SkillVersion.skill_id.in_(skills.values())))
        await session.execute(delete(SkillNode).where(SkillNode.id.in_(skills.values())))
        await session.execute(delete(AiProvider).where(AiProvider.id==provider.id)); await session.commit()
    await engine.dispose()
    print('Historical backfill: selection, frozen preview, reuse, recheck, targets, source revisions and queue passed')


asyncio.run(main())
