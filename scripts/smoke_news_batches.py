"""Exercise durable batch barriers in the disposable CI database without model calls."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from sqlalchemy import select, delete, insert, func, text
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Batch, BatchPackage, BatchEvent, Package, Event, Membership, Attempt
from alphapulse.news_research import batches, service
from alphapulse.news_research.api import retry_event


async def main():
    url = urlparse(os.environ['DATABASE_URL'])
    assert (url.username, url.password, url.path) == ('ci', 'ci-only', '/ci')
    now = datetime.now(UTC)
    settings = SimpleNamespace(start_at=now - timedelta(days=1))
    async with SessionLocal() as s:
        assert await s.scalar(select(Batch.id).limit(1)) is None
        def news(key):
            return NewsItem(source_code='wscn_news', external_id='batch-'+key,
                title=key, summary='', content_text=key, content_hash='a'*64,
                url='https://example.invalid/'+key, published_at=now)
        originals = [news(str(i)) for i in range(3)]
        s.add_all(originals)
        await s.commit()
        batch = await batches.freeze(s, settings)
        identifier = batch.id
        assert batch.phase == 'assigning'
        packages = list(await s.scalars(select(Package).order_by(Package.id)))
        assert len(packages) == 3
        later = news('later')
        s.add(later)
        await s.commit()
        actions = await batches.actions(s, batch, set(), 8)
        assert len(actions) == 3 and all(stage == 'index' for stage, _ in actions)
        for p in packages:
            p.state = 'assign_pending'
        await s.commit()
        actions = await batches.actions(s, batch, set(), 8)
        assert len(actions) == 1 and actions[0][0] == 'assign'
        assert not await batches.actions(s, batch, {actions[0]}, 8)
        event = Event(title='One shared event', tags=[], revision=3, state='pending', due_at=now)
        s.add(event)
        await s.flush()
        for p in packages[:2]:
            p.state = 'done'
            s.add(Membership(event_id=event.id, package_id=p.id, evidence='original', reason='same event', change_kind='new_fact'))
        s.add(Attempt(package_id=packages[0].id, content_hash=packages[0].content_hash, stage='assign', state='done', input={}, rules={'membership_targets': {str(event.id): {'urgent': True}}}))
        packages[2].state = 'index_failed'
        await s.commit()
        assert not await batches.actions(s, batch, {('assign', packages[0].id)}, 8)
        assert batch.phase == 'assigning'
        assert not await batches.actions(s, batch, set(), 8)
        await s.commit()
        assert batch.phase == 'researching'
        assert (await s.get(BatchEvent, (batch.id, event.id))).state == 'urgent_pending'
        # Reopen the session to prove the admission barrier is durable.
    async with SessionLocal() as s:
        batch = await s.get(Batch, identifier)
        actions = await batches.actions(s, batch, set(), 8)
        assert actions == [('event', event.id)]
        await s.commit()
        assert not await batches.actions(s, batch, set(actions), 8)
        e = await s.get(Event, event.id)
        e.state = 'done'
        await s.commit()
        assert not await batches.actions(s, batch, set(), 8)
        await s.commit()
        assert batch.phase == 'partial'
        progress = await batches.progress(s)
        assert progress['packages_failed'] == 1 and progress['events_done'] == 1
        next_batch = await batches.freeze(s, settings)
        items = list(await s.scalars(select(BatchPackage).where(BatchPackage.batch_id == next_batch.id)))
        assert len(items) == 1
        p = await s.get(Package, items[0].package_id)
        assert p.news_id == later.id
        original = await s.get(NewsItem, later.id)
        original.content_hash = 'b'*64
        await s.commit()
        assert not await batches.actions(s, next_batch, set(), 8)
        await s.commit()
        assert items[0].state == 'deferred'
        # Leave no fixtures for other isolated acceptance scripts.
        for model in (BatchEvent, BatchPackage, Batch, Membership, Attempt, Package, Event, NewsItem):
            await s.execute(delete(model))
        await s.commit()
    await backfill_regressions(now)
    await carryover_regressions(now)
    await regressions(now, settings)
    await engine.dispose()
    print('Frozen batches passed: assignment barrier, single research, failures, new arrivals, restart and source revisions.')


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
        report = await batches.coverage(s, start, now)
        assert {k: report[k] for k in ['total', 'completed', 'missing', 'stale', 'pending', 'processing', 'failed', 'unprocessed']} == {
            'total': 8, 'completed': 1, 'missing': 1, 'stale': 1, 'pending': 2,
            'processing': 1, 'failed': 2, 'unprocessed': 7}, report
        assert await s.scalar(select(func.count()).select_from(Package)) == before
        # Historical news arriving after a scan must still enter the next frozen batch.
        late = original('late-arrival', start+timedelta(hours=1))
        s.add(late)
        packages['processing'].state = 'index_pending'
        await s.commit()
        report = await batches.coverage(s, start, now)
        assert report['total'] == 9 and report['missing'] == 2
        batch = await batches.freeze(s, SimpleNamespace(start_at=start))
        admitted = set(await s.scalars(select(Package.news_id).join(BatchPackage).where(BatchPackage.batch_id == batch.id)))
        assert {news['boundary'].id, late.id} <= admitted
        assert not {news['done'].id, older.id, future.id} & admitted
        assert packages['stale'].state == 'index_pending'
        assert packages['stale'].content_hash == news['stale'].content_hash
        # Moving the start backwards discovers originals missed by the former boundary.
        expanded = await batches.freeze(s, SimpleNamespace(start_at=older.published_at))
        admitted = set(await s.scalars(select(Package.news_id).join(BatchPackage).where(BatchPackage.batch_id == expanded.id)))
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
        wide = await batches.coverage(s, start-timedelta(days=2), now)
        assert wide['outside']['unprocessed'] == 1  # Explicitly enrolled future original.
        narrow = await batches.coverage(s, start, now)
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


async def regressions(now, settings):
    async def clear(session):
        # This script runs first in an empty, disposable CI database. TRUNCATE avoids
        # quadratic foreign-key checks when removing the 33,000-row stress fixture.
        await session.execute(text('TRUNCATE news_research_batches, news_research_packages, news_research_events, news_items CASCADE'))
        await session.commit()

    async with SessionLocal() as s:
        event = Event(title='Deferred retry', tags=[], revision=7, state='pending', due_at=now)
        previous = Batch(phase='partial', cutoff_at=now)
        s.add_all([event, previous])
        await s.flush()
        s.add(BatchEvent(batch_id=previous.id, event_id=event.id, revision=7, state='deferred'))
        await s.commit()
        assert await batches.freeze(s, settings) is None
        await retry_event(event.id, s)
        assert event.revision == 8
        batch = await batches.freeze(s, settings)
        assert batch is not None
        await batches.actions(s, batch, set(), 8)
        await s.commit()
        assert await batches.actions(s, batch, set(), 8) == [('event', event.id)]
        # Failed retries retain their revision so cached evidence can still be reused.
        event.state = 'failed'
        await s.commit()
        await retry_event(event.id, s)
        assert event.revision == 8 and event.state == 'pending'
        await clear(s)

        def original(key, published_at):
            return dict(source_code='wscn_news', external_id=key, title=key, summary='',
                content_text='Original', content_hash='a'*64, url='https://example.invalid/'+key,
                published_at=published_at)
        news = NewsItem(**original('manual-history', now-timedelta(days=365)))
        excluded = NewsItem(**original('unselected-history', now-timedelta(days=365)))
        s.add_all([news, excluded])
        await s.commit()
        await service.enroll(s, [news.id])
        batch = await batches.freeze(s, settings)
        package = await s.scalar(select(Package).where(Package.news_id == news.id))
        news.content_hash = 'b'*64
        await s.commit()
        await batches.actions(s, batch, set(), 8)
        await s.commit()
        await batches.actions(s, batch, set(), 8)
        await s.commit()
        assert batch.phase == 'partial'
        batch = await batches.freeze(s, settings)
        assert package.content_hash == news.content_hash and package.state == 'index_pending'
        assert await batches.actions(s, batch, set(), 8) == [('index', package.id)]
        assert await s.scalar(select(func.count()).select_from(Package)) == 1
        # A previously completed manual original also refreshes outside the automatic boundary.
        package.state = 'done'
        news.content_hash = 'c'*64
        await s.commit()
        await batches.freeze(s, settings)
        assert package.content_hash == news.content_hash and package.state == 'index_pending'
        await clear(s)

        count = 33000
        await s.execute(insert(NewsItem), [original('bulk-'+str(i), now) for i in range(count)])
        await s.commit()
        batch = await batches.freeze(s, settings)
        assert await s.scalar(select(func.count()).select_from(BatchPackage).where(BatchPackage.batch_id == batch.id)) == count
        assert await s.scalar(select(func.count()).select_from(Package)) == count
        # Exercise the existing-pending-ID and membership arrays above the driver limit too.
        next_batch = await batches.freeze(s, settings)
        assert await s.scalar(select(func.count()).select_from(BatchPackage).where(BatchPackage.batch_id == next_batch.id)) == count
        assert len(await batches.actions(s, next_batch, set(), 8)) == 8
        await clear(s)
    print('Batch regressions passed: deferred retries, failed-evidence reuse, manual source revisions, and 33,000 originals without parameter overflow.')


asyncio.run(main())
