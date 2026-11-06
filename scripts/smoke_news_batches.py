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
    await queue_review_regressions(now, settings)
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
        original = NewsItem(source_code='wscn_news', external_id='retry-member', title='retry', summary='', content_text='retry', content_hash='a'*64, url='https://example.invalid/retry', published_at=now)
        s.add_all([event, previous, original])
        await s.flush()
        member = Package(news_id=original.id, content_hash=original.content_hash, state='done', keywords=[])
        s.add(member)
        await s.flush()
        s.add(Membership(event_id=event.id, package_id=member.id, evidence='retry', reason='retry', change_kind='new_fact'))
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
        assert await batches.freeze(s, settings) is None
        package = await s.scalar(select(Package).where(Package.news_id == news.id))
        news.content_hash = 'b'*64
        await s.commit()
        assert await batches.freeze(s, settings) is None
        assert package.content_hash != news.content_hash
        expanded = SimpleNamespace(start_at=news.published_at)
        batch = await batches.freeze(s, expanded)
        assert package.content_hash == news.content_hash and package.state == 'index_pending'
        assert ('index', package.id) in await batches.actions(s, batch, set(), 8)
        await clear(s)

        count = 33000
        await s.execute(insert(NewsItem), [original('bulk-'+str(i), now) for i in range(count)])
        await s.commit()
        batch = await batches.freeze(s, settings)
        assert await s.scalar(select(func.count()).select_from(BatchPackage).where(BatchPackage.batch_id == batch.id)) == 30
        assert await s.scalar(select(func.count()).select_from(Package)) == 30
        # Exercise the existing-pending-ID and membership arrays above the driver limit too.
        next_batch = await batches.freeze(s, settings)
        assert await s.scalar(select(func.count()).select_from(BatchPackage).where(BatchPackage.batch_id == next_batch.id)) == 30
        assert len(await batches.actions(s, next_batch, set(), 8)) == 8
        await clear(s)
    print('Batch regressions passed: deferred retries, failed-evidence reuse, manual source revisions, and 33,000 originals without parameter overflow.')


async def queue_review_regressions(now, settings):
    async def clear(session):
        await session.execute(text('TRUNCATE news_research_batches, news_research_packages, news_research_events, news_items CASCADE'))
        await session.commit()

    async with SessionLocal() as s:
        news = NewsItem(source_code='wscn_news', external_id='review-barrier', title='historical',
                        summary='', content_text='historical', content_hash='a'*64,
                        url='https://example.invalid/review-barrier', published_at=now)
        s.add(news)
        await s.flush()
        package = Package(news_id=news.id, content_hash=news.content_hash, state='assign_pending', keywords=[])
        event = Event(title='historical event', tags=[], revision=1, state='pending', due_at=now)
        batch = Batch(phase='assigning', cutoff_at=now)
        s.add_all([package, event, batch])
        await s.flush()
        s.add(Membership(event_id=event.id, package_id=package.id,
                         evidence='historical', reason='same event', change_kind='new_fact'))
        s.add(BatchPackage(batch_id=batch.id, package_id=package.id, content_hash=package.content_hash, state='pending'))
        await s.commit()
        unrelated = package.id + 1000
        # Assignment remains globally serial even when the other call is not in this batch.
        assert await batches.actions(s, batch, {('assign', unrelated)}, 7) == []
        assert batch.phase == 'assigning'
        # Active work on this batch still holds its assignment barrier.
        package.state = 'done'
        await s.commit()
        assert await batches.actions(s, batch, {('assign', package.id)}, 7) == []
        assert batch.phase == 'assigning'
        # Unrelated realtime package and event calls must not hold the barrier.
        realtime = {('index', unrelated), ('event', event.id + 1000)}
        assert await batches.actions(s, batch, realtime, 6) == []
        await s.commit()
        assert batch.phase == 'researching'
        assert await batches.actions(s, batch, realtime, 6) == [('event', event.id)]
        await s.commit()
        event.state = 'done'
        await s.commit()
        # A completed result can be visible before the owning task has returned.
        assert await batches.actions(s, batch, realtime | {('event', event.id)}, 5) == []
        assert batch.phase == 'researching'
        assert await batches.actions(s, batch, realtime | {('index', package.id)}, 5) == []
        assert batch.phase == 'researching'
        assert await batches.actions(s, batch, realtime, 6) == []
        await s.commit()
        assert batch.phase == 'done'
        # Completed batches release admission of the next historical originals.
        news2 = NewsItem(source_code='wscn_news', external_id='review-next', title='next', summary='',
                         content_text='next', content_hash='b'*64,
                         url='https://example.invalid/review-next', published_at=now)
        s.add(news2)
        await s.commit()
        assert (await batches.freeze(s, settings)).id != batch.id
        await clear(s)

        # Thirty manual-only deferred heads cannot consume the thirty runnable slots.
        news = NewsItem(source_code='wscn_news', external_id='review-deferred', title='source', summary='',
                        content_text='source', content_hash='a'*64,
                        url='https://example.invalid/review-deferred', published_at=now)
        s.add(news)
        await s.flush()
        package = Package(news_id=news.id, content_hash=news.content_hash, state='done', keywords=[])
        previous = Batch(phase='partial', cutoff_at=now)
        events = [Event(title='deferred-head-'+str(i), tags=[], revision=7, state='pending',
                        due_at=now-timedelta(minutes=35-i)) for i in range(35)]
        s.add_all([package, previous, *events])
        await s.flush()
        for i, event in enumerate(events):
            s.add(Membership(event_id=event.id, package_id=package.id, evidence='source', reason='same source', change_kind='new_fact'))
            if i <= 30:
                s.add(BatchEvent(batch_id=previous.id, event_id=event.id,
                                 revision=7 if i < 30 else 6, state='deferred'))
        await s.commit()
        batch = await batches.freeze(s, settings)
        assert batch is not None
        selected = set(await s.scalars(select(BatchEvent.event_id).where(BatchEvent.batch_id == batch.id)))
        assert selected == {event.id for event in events[30:]}, selected
        assert all(event.state == 'pending' and event.revision == 7 for event in events[:30])
        await batches.actions(s, batch, set(), 8)
        await s.commit()
        actions = await batches.actions(s, batch, set(), 8)
        assert set(actions) == {('event', event.id) for event in events[30:]}
        await clear(s)
    print('Queue review regressions passed: unrelated realtime calls allow both batch barriers to advance, owning calls still block, assignment stays serial, and deferred heads cannot starve runnable events.')


asyncio.run(main())
