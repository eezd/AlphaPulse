"""Validate continuous scheduling with synthetic PostgreSQL data and no model calls."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch
from sqlalchemy import select, text, func, delete
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Package, Event, Membership, Attempt, Version, Batch, BatchEvent, BatchPackage
from alphapulse.news_research import scheduling, service, coverage as reports
from alphapulse.news_research.api import retry_event


async def main():
    url = urlparse(os.environ['DATABASE_URL'])
    assert (url.username, url.password, url.path) == ('ci', 'ci-only', '/ci')
    now = datetime.now(UTC)
    config = SimpleNamespace(start_at=now-timedelta(days=5), realtime_from=now-timedelta(hours=1),
                             config={'concurrency': 8}, enabled=True)
    async with SessionLocal() as s:
        async def clear():
            await s.execute(text('TRUNCATE news_research_batches, news_research_packages, news_research_events, news_items CASCADE'))
            await s.commit()
            s.expunge_all()

        def original(i, live=False):
            return NewsItem(source_code='wscn_news', external_id='dynamic-'+str(i), title='source', summary='',
                content_text='source', content_hash='a'*64, url='https://example.invalid/'+str(i),
                published_at=now-timedelta(minutes=30) if live else now-timedelta(days=3)+timedelta(seconds=i))

        # More than thirty originals: refill while seven older calls remain active.
        s.add_all([original(i) for i in range(80)])
        await s.commit()
        actions = await scheduling.actions(s, config, set(), 8)
        assert len(actions) == 8 and all(stage == 'index' for stage, _ in actions)
        first = await s.get(Package, actions[0][1]); first.state='assign_pending'
        await s.commit()
        active=set(actions[1:])
        selected=await scheduling.actions(s, config, active, 1)
        assert selected == [('assign', first.id)]
        first.state='done'; await s.commit()
        # Each completion admits another original, independent of the slow seven.
        for _ in range(35):
            selected=await scheduling.actions(s, config, active, 1)
            assert len(selected)==1 and selected[0][0]=='index'
            p=await s.get(Package, selected[0][1]); p.state='done'
            await s.commit()
        assert await s.scalar(select(func.count()).select_from(Package)) > 30
        assert await s.scalar(select(func.count()).select_from(Batch)) == 0
        status=await scheduling.queue_status(s, config)
        assert status['backfill']['missing'] > 0 and status['capacity']==8
        report=await reports.coverage(s, config.start_at)
        assert report['total']==80 and report['unprocessed']==80
        await clear()

        # One serial assignment plus seven ready studies, not an entire batch barrier.
        news=[original(i) for i in range(60)]
        s.add_all(news); await s.flush()
        packages=[Package(news_id=n.id,content_hash=n.content_hash,state='done',keywords=[]) for n in news]
        s.add_all(packages); await s.flush()
        events=[Event(title='event-'+str(i),tags=[],revision=1,state='pending',due_at=now-timedelta(minutes=1)) for i in range(54)]
        s.add_all(events); await s.flush()
        for e,p in zip(events,packages,strict=False):
            s.add(Membership(event_id=e.id,package_id=p.id,evidence='source',reason='source',change_kind='new_fact'))
        packages[54].state='assign_pending'
        packages[55].state='assign_pending'
        await s.commit()
        selected=await scheduling.actions(s, config, set(), 8)
        assert selected[0]==('assign',packages[54].id)
        assert len(selected)==8 and sum(stage=='event' for stage,_ in selected)==7
        # Another study fills a freed slot even while that assignment remains active.
        active=set(selected[:-1])
        refill=await scheduling.actions(s, config, active, 1)
        assert len(refill)==1 and refill[0][0]=='event' and refill[0] not in active
        # Realtime joins the next free slot; zero capacity never enrolls work.
        live=original(100,True); s.add(live); await s.commit()
        assert await scheduling.actions(s, config, active, 0)==[]
        assert await s.scalar(select(Package.id).where(Package.news_id==live.id)) is None
        refill=await scheduling.actions(s, config, active, 1)
        live_package=await s.scalar(select(Package).where(Package.news_id==live.id))
        assert refill==[('index',live_package.id)]
        # Stale source enrollment happens only after its existing call is released.
        live_package.state='index_processing'
        s.add(Attempt(package_id=live_package.id,stage='index',state='processing',content_hash='a'*64,input={},rules={}))
        live.content_hash='b'*64; await s.commit()
        assert await scheduling.realtime_actions(s,config,set(),8)==[]
        await clear()

        # Failed/deferred heads must be filtered before LIMIT, including realtime.
        n=original(1); s.add(n); await s.flush()
        p=Package(news_id=n.id,content_hash=n.content_hash,state='done',keywords=[]); s.add(p); await s.flush()
        old=Batch(phase='partial',cutoff_at=now); s.add(old); await s.flush()
        events=[Event(title='manual-'+str(i),tags=[],revision=7,state='pending',due_at=now-timedelta(minutes=1)) for i in range(45)]
        s.add_all(events); await s.flush()
        for i,e in enumerate(events):
            s.add(Membership(event_id=e.id,package_id=p.id,evidence='source',reason='source',change_kind='new_fact'))
            if i<35: s.add(BatchEvent(batch_id=old.id,event_id=e.id,revision=7,state='deferred'))
        s.add(Version(event_id=events[35].id,revision=7,state='superseded',input={},rules={}))
        events[36].due_at=now+timedelta(minutes=5)
        events[37].state='failed'
        await s.commit()
        selected=await scheduling.actions(s,config,set(),8)
        assert {i for stage,i in selected}=={e.id for e in events[38:]}
        status=await scheduling.queue_status(s,config)
        assert status['events']['backfill']=={'manual_review':36,'aggregating':1,'failed':1,'ready':7}
        assert 'aggregation' in status['idle_reasons'] and 'manual_retry' in status['idle_reasons']
        # Final claim independently enforces manual retry, not only selection.
        with patch.object(service, 'model_config', side_effect=AssertionError('Must not call model')):
            await service.process_event(events[35].id,{})
        await retry_event(events[35].id,s)
        assert events[35].revision==8
        assert ('event',events[35].id) in await scheduling.actions(s,config,set(),8)
        # Moving to a new revision naturally releases legacy deferred records.
        events[0].revision=8; events[0].realtime_pending=True
        await s.commit()
        assert (await scheduling.actions(s,config,set(),1))==[('event',events[0].id)]
        await clear()

        # Failed earlier news does not block; a slow earlier index does block later assignment.
        news=[original(i) for i in range(4)]; s.add_all(news); await s.flush()
        ps=[Package(news_id=n.id,content_hash=n.content_hash,state=state,keywords=[]) for n,state in zip(news,
            ['index_failed','index_processing','assign_pending','index_pending'],strict=True)]
        s.add_all(ps); await s.commit()
        selected=await scheduling.actions(s,config,{('index',ps[1].id)},7)
        assert selected==[('index',ps[3].id)]
        ps[1].state='assign_pending'; await s.commit()
        selected=await scheduling.actions(s,config,set(),8)
        assert selected[0]==('assign',ps[1].id) and sum(stage=='assign' for stage,_ in selected)==1
        config.start_at=now-timedelta(days=1)
        assert await scheduling.actions(s,config,set(),8)==[]
        config.start_at=now-timedelta(days=5)
        assert await scheduling.actions(s,config,set(),8)
        await clear()
    # A pending revision cannot run while the preceding revision is still paid/in flight.
    async with SessionLocal() as s:
        n=original(1,True); s.add(n); await s.flush()
        p=Package(news_id=n.id,content_hash=n.content_hash,state='done',keywords=[])
        e=Event(title='changing event',tags=[],revision=2,state='pending',realtime_pending=True,
                due_at=now-timedelta(minutes=1))
        s.add_all([p,e]); await s.flush()
        s.add(Membership(event_id=e.id,package_id=p.id,evidence='source',reason='source',change_kind='new_fact'))
        version=Version(event_id=e.id,revision=1,state='processing',input={},rules={})
        s.add(version); await s.commit()
        status=await scheduling.queue_status(s,config)
        assert status['running']==1 and status['events']['realtime']=={'waiting_previous':1}
        assert status['idle_reasons']==['waiting_previous']
        assert await scheduling.actions(s,config,set(),7)==[]
        from alphapulse.news_research.api import event as event_detail
        detail=await event_detail(e.id,s)
        assert detail.data['wait_reason']=='等待上一轮研究完成后处理事件新变化'
        # Releasing the old call automatically makes the new revision ready again.
        version.state='superseded'; await s.commit()
        status=await scheduling.queue_status(s,config)
        assert status['running']==0 and status['events']['realtime']=={'ready':1}
        assert await scheduling.actions(s,config,set(),8)==[('event',e.id)]
        # Apply the same guard to historical events, independent of the priority lane.
        e.realtime_pending=False; version.state='processing'; await s.commit()
        status=await scheduling.queue_status(s,config)
        assert status['events']['backfill']=={'waiting_previous':1}
        assert await scheduling.actions(s,config,set(),8)==[]
        await s.execute(text('TRUNCATE news_research_settings, news_research_packages, news_research_events, news_items CASCADE'))
        await s.commit()
    # Large history scans remain bounded by free capacity, without huge IN lists.
    async with SessionLocal() as s:
        await s.execute(text("""INSERT INTO news_items
            (source_code,external_id,url,title,summary,content_html,content_text,tags,is_paywalled,
             published_at,content_hash,revision_no,extra)
            SELECT 'wscn_news','dynamic-stress-'||i,'https://example.invalid/stress','source','','','source',
             '[]'::jsonb,false,:published,:hash,1,'{}'::jsonb FROM generate_series(1,33000) i"""),
            {'published':now-timedelta(days=2),'hash':'a'*64})
        await s.commit()
        for capacity in [1,3,8,16]:
            selected=await scheduling.actions(s,config,set(),capacity)
            assert len(selected)==capacity and all(stage=='index' for stage,_ in selected)
            assert await s.scalar(select(func.count()).select_from(Package)) <= 16
        await s.execute(text('TRUNCATE news_research_packages, news_items CASCADE'))
        await s.commit()
    await backfill_regressions(now)
    await carryover_regressions(now)
    await engine.dispose()
    print('Dynamic queue passed: continuous refill beyond 30 originals, serial assignment with 54 ready studies, realtime priority, stale active calls, chronology, coverage and manual retry barriers.')


async def backfill_regressions(now):
    start = now - timedelta(days=2)
    async with SessionLocal() as s:
        def original(key, published_at):
            return NewsItem(source_code='wscn_news', external_id='coverage-'+key,
                title=key, summary='', content_text=key, content_hash='a'*64,
                url='https://example.invalid/'+key, published_at=published_at)
        keys = ['boundary', 'done', 'research-pending', 'research-failed',
                'index-failed', 'processing', 'assign-pending', 'stale']
        news = {key: original(key, start if key == 'boundary' else now) for key in keys}
        older = original('older', start-timedelta(microseconds=1))
        future = original('future', now+timedelta(days=1))
        s.add_all([*news.values(), older, future])
        await s.flush()
        states = {'index-failed': 'index_failed', 'processing': 'index_processing',
                  'assign-pending': 'assign_pending'}
        packages = {key: Package(news_id=row.id, content_hash='b'*64 if key == 'stale' else row.content_hash,
                                state=states.get(key, 'done'), keywords=[])
                    for key, row in news.items() if key != 'boundary'}
        events = {state: Event(title='coverage-'+state, state=state, tags=[], revision=1,
                              result={} if state == 'done' else None, due_at=now)
                  for state in ['done', 'pending', 'failed']}
        s.add_all([*packages.values(), *events.values()])
        await s.flush()
        for key, state in [('done', 'done'), ('research-pending', 'pending'),
                           ('research-failed', 'failed'), ('research-failed', 'done'), ('stale', 'done')]:
            s.add(Membership(package_id=packages[key].id, event_id=events[state].id,
                             evidence=key, reason='coverage fixture', change_kind='new_fact'))
        await s.commit()
        before = await s.scalar(select(func.count()).select_from(Package))
        report = await reports.coverage(s, start, now)
        assert {k: report[k] for k in ['total', 'completed', 'missing', 'stale', 'pending', 'processing', 'failed', 'unprocessed']} == {
            'total': 8, 'completed': 1, 'missing': 1, 'stale': 1, 'pending': 2,
            'processing': 1, 'failed': 2, 'unprocessed': 7}, report
        assert await s.scalar(select(func.count()).select_from(Package)) == before
        # Late historical arrivals remain discoverable without a fixed cutoff.
        late = original('late-arrival', start+timedelta(hours=1))
        s.add(late)
        packages['processing'].state = 'index_pending'
        await s.commit()
        report = await reports.coverage(s, start, now)
        assert report['total'] == 9 and report['missing'] == 2
        config = SimpleNamespace(start_at=start, realtime_from=None)
        selected = await scheduling.actions(s, config, set(), 16)
        admitted = set(await s.scalars(select(Package.news_id).where(Package.id.in_([i for stage,i in selected if stage=='index']))))
        assert {news['boundary'].id, late.id} <= admitted
        assert not {news['done'].id, older.id, future.id} & admitted
        assert packages['stale'].state == 'index_pending'
        assert packages['stale'].content_hash == news['stale'].content_hash
        config.start_at=older.published_at
        selected=await scheduling.actions(s, config, set(), 16)
        admitted=set(await s.scalars(select(Package.news_id).where(Package.id.in_([i for stage,i in selected if stage=='index']))))
        assert older.id in admitted and future.id not in admitted
        assert all(row.content_hash == 'a'*64 for row in [*news.values(), older, future, late])
        for model in (BatchEvent, BatchPackage, Batch, Membership, Attempt, Package, Event, NewsItem):
            await s.execute(delete(model))
        await s.commit()
    print('Backfill coverage passed: inclusive boundary, pending research, distinct news counts, late arrivals, source revisions and expanded history without model calls.')

async def carryover_regressions(now):
    start = now - timedelta(days=2)
    async with SessionLocal() as s:
        keys = ['inside', 'pending', 'processing', 'failed', 'stale', 'research', 'done', 'unselected', 'future']
        news = {key: NewsItem(source_code='wscn_news', external_id='carryover-'+key,
                             title=key, summary='', content_text=key, content_hash='a'*64,
                             url='https://example.invalid/'+key,
                             published_at=now if key == 'inside' else
                             now+timedelta(days=1) if key == 'future' else start-timedelta(days=1))
                for key in keys}
        s.add_all(news.values())
        await s.flush()
        states = {'pending': 'index_pending', 'processing': 'assign_processing',
                  'failed': 'index_failed', 'future': 'assign_pending'}
        packages = {key: Package(news_id=row.id, content_hash='b'*64 if key == 'stale' else row.content_hash,
                                state=states.get(key, 'done'), keywords=[])
                    for key, row in news.items() if key != 'unselected'}
        done = Event(title='carryover-done', tags=[], state='done', result={}, revision=1, due_at=now)
        pending = Event(title='carryover-pending', tags=[], state='pending', revision=1, due_at=now)
        batch = Batch(phase='assigning', cutoff_at=now)
        s.add_all([*packages.values(), done, pending, batch])
        await s.flush()
        for key, event in [('inside', done), ('done', done), ('research', done), ('research', pending)]:
            s.add(Membership(package_id=packages[key].id, event_id=event.id,
                             evidence=key, reason='carryover fixture', change_kind='new_fact'))
        for key in ['pending', 'processing', 'stale']:
            s.add(BatchPackage(batch_id=batch.id, package_id=packages[key].id,
                               content_hash=packages[key].content_hash, state='pending'))
        s.add(BatchEvent(batch_id=batch.id, event_id=pending.id, revision=1, state='pending'))
        await s.commit()
        wide = await reports.coverage(s, start-timedelta(days=2), now)
        assert wide['outside']['unprocessed'] == 1  # Explicitly enrolled future original.
        narrow = await reports.coverage(s, start, now)
        assert narrow['total'] == 1 and narrow['completed'] == 1 and narrow['unprocessed'] == 0
        assert narrow['outside'] == {'unprocessed': 6, 'pending': 3, 'processing': 1, 'failed': 1, 'stale': 1}, narrow
        assert await s.scalar(select(func.count()).select_from(BatchPackage)) == 3
        assert await s.scalar(select(func.count()).select_from(BatchEvent)) == 1
        assert await s.scalar(select(func.count()).select_from(Package)) == 8
        assert batch.phase == 'assigning' and packages['pending'].state == 'index_pending'
        for model in (BatchEvent, BatchPackage, Batch, Membership, Attempt, Package, Event, NewsItem):
            await s.execute(delete(model))
        await s.commit()
    print('Carryover coverage passed: narrowing preserves and reports outside queued news, active work, changed originals, pending research and explicit future enrollment without counting completed or unselected originals.')

asyncio.run(main())
