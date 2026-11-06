"""Exercise preemption and recovery using disposable PostgreSQL, never a model."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from sqlalchemy import select, text, func
from sqlalchemy.exc import IntegrityError
from unittest.mock import patch
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Package, Event, Membership, Attempt, Version, ResearchSettings
from alphapulse.news_research import scheduling, service
from alphapulse.news_research.worker import recover_interrupted


async def main():
    url = urlparse(os.environ['DATABASE_URL'])
    assert (url.username, url.password, url.path) == ('ci', 'ci-only', '/ci')
    now = datetime.now(UTC)
    anchor = now - timedelta(hours=1)
    config = SimpleNamespace(start_at=now-timedelta(days=5), realtime_from=anchor)
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).select_from(Package)) == 0
        def original(key, published):
            return NewsItem(source_code='wscn_news', external_id='priority-'+key, title=key, summary='',
                            content_text=key, content_hash='a'*64, url='https://example.invalid/'+key, published_at=published)
        old = original('old', now-timedelta(days=4))
        live = original('live', now-timedelta(minutes=30))
        late = original('late', now-timedelta(days=3))
        s.add_all([old, live, late]); await s.commit()
        await scheduling.lane_actions(s, config, set(), 8, False)
        packages = list(await s.scalars(select(Package)))
        assert len(packages) == 2 and all(p.news_id != live.id for p in packages)
        actions = await scheduling.realtime_actions(s, config, {('index', packages[0].id)}, 1)
        live_package = await s.scalar(select(Package).where(Package.news_id == live.id))
        assert actions == [('index', live_package.id)]
        assert await scheduling.realtime_actions(s, config, set(), 0) == []
        # Shrinking the history range must not exclude accumulated realtime news.
        config.start_at = now-timedelta(minutes=5)
        actions = await scheduling.realtime_actions(s, config, set(), 1)
        assert actions == [('index', live_package.id)]
        assert not await scheduling.lane_actions(s, config, set(), 8, False)
        config.start_at = now-timedelta(days=5)
        assert await scheduling.lane_actions(s, config, set(), 8, False)
        assert packages[0].keywords == []
        # A realtime event's historical dependencies inherit priority.
        event = Event(title='shared', tags=[], state='pending', revision=1, realtime_pending=True, due_at=now)
        s.add(event); await s.flush()
        for p in [packages[0], live_package]:
            s.add(Membership(event_id=event.id, package_id=p.id, evidence='fixture', reason='fixture', change_kind='new_fact'))
        packages[0].state = 'assign_pending'
        live_package.state = 'done'
        await s.commit()
        config.start_at = now-timedelta(minutes=5)
        assert await scheduling.realtime_actions(s, config, set(), 1) == []
        config.start_at = now-timedelta(days=5)
        assert await scheduling.realtime_actions(s, config, set(), 1) == [('assign', packages[0].id)]
        packages[0].state = 'done'; await s.commit()
        assert await scheduling.realtime_actions(s, config, set(), 1) == [('event', event.id)]
        event.due_at = now+timedelta(minutes=5); await s.commit()
        assert ('event', event.id) not in await scheduling.realtime_actions(s, config, set(), 8)
        # Pausing invalidates a selected but not-yet-claimed task.
        s.add(ResearchSettings(id=1, config={}, enabled=False, control_revision=9, start_at=config.start_at, realtime_from=anchor))
        await s.commit()
        assert await scheduling.control(s, {'_control_revision': 8}) is False
        await s.commit()
        with patch.object(service, 'complete', side_effect=AssertionError('Paused selection must not call a model')):
            await service.process_package(live_package.id, {'_control_revision': 8})
        assert await s.scalar(select(func.count()).select_from(Attempt)) == 0
        live_package = await s.get(Package, live_package.id)
        live_package.state = 'index_processing'
        s.add(Attempt(package_id=live_package.id, content_hash='a'*64, stage='index', state='processing', input={}, rules={}))
        event = await s.get(Event, event.id); event.state='processing'
        s.add(Version(event_id=event.id, revision=event.revision, state='processing', input={}, rules={}))
        await s.commit()
        for duplicate in [
            Attempt(package_id=live_package.id, content_hash='a'*64, stage='assign', state='processing', input={}, rules={}),
            Version(event_id=event.id, revision=event.revision, state='processing', input={}, rules={}),
        ]:
            try:
                async with s.begin_nested():
                    s.add(duplicate)
                    await s.flush()
            except IntegrityError:
                pass
            else:
                raise AssertionError('Concurrent duplicate claim was accepted')
    await recover_interrupted()
    async with SessionLocal() as s:
        assert (await s.get(Package, live_package.id)).state == 'index_failed'
        assert (await s.get(Event, event.id)).state == 'failed'
        assert await scheduling.realtime_actions(s, config, set(), 8) == []
        empty = Event(title='removed members', tags=[], state='pending', revision=1, realtime_pending=True, due_at=now)
        s.add(empty); await s.commit()
        assert ('event', empty.id) in await scheduling.realtime_actions(s, config, set(), 8)
        with patch.object(service.Research, 'run', side_effect=AssertionError('Empty events must not call a model')):
            await service.process_event(empty.id, {})
        await s.refresh(empty)
        assert empty.state == 'retired'
        await s.execute(text('TRUNCATE news_research_settings, news_research_batches, news_research_packages, news_research_events, news_items CASCADE'))
        await s.commit()
    await engine.dispose()
    print('Priority queue passed: live preemption, stable boundary, late history, range changes, dependencies, aggregation, stale control and uncertain-call recovery.')

asyncio.run(main())
