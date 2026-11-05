"""Exercise durable batch barriers in the disposable CI database without model calls."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from sqlalchemy import select, delete
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Batch, BatchPackage, BatchEvent, Package, Event, Membership, Attempt
from alphapulse.news_research import batches


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
    await engine.dispose()
    print('Frozen batches passed: assignment barrier, single research, failures, new arrivals, restart and source revisions.')


asyncio.run(main())
