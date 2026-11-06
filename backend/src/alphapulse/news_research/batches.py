"""Durable admission barriers: finish this batch's assignments before any research."""
from collections import Counter
from datetime import UTC, datetime
from sqlalchemy import select, or_, and_, exists, func, any_, case
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Batch, BatchPackage, BatchEvent, Package, Event, Membership, Attempt
from alphapulse.news_research.service import enroll
from alphapulse.news_research.scheduling import scoped_event, ready_event


async def coverage(session, start_at, cutoff=None):
    """Count each original once, including unfinished research after assignment."""
    cutoff = cutoff or datetime.now(UTC)
    research = select(
        Membership.package_id,
        func.bool_or(Event.state == 'failed').label('failed'),
        func.bool_or(Event.state == 'processing').label('processing'),
        func.bool_or(Event.state.not_in(['done', 'retired'])).label('unfinished'),
    ).join(Event, Event.id == Membership.event_id).group_by(Membership.package_id).subquery()
    state = case(
        (Package.id.is_(None), 'missing'),
        (Package.content_hash != NewsItem.content_hash, 'stale'),
        (Package.state.in_(['index_failed', 'assign_failed']), 'failed'),
        (Package.state.in_(['index_processing', 'assign_processing']), 'processing'),
        (Package.state != 'done', 'pending'),
        (research.c.failed, 'failed'),
        (research.c.processing, 'processing'),
        (or_(research.c.package_id.is_(None), research.c.unfinished), 'pending'),
        else_='completed',
    )
    in_range = and_(NewsItem.published_at >= start_at, NewsItem.published_at <= cutoff)
    rows = (await session.execute(
        select(in_range, state, func.count()).select_from(NewsItem)
        .outerjoin(Package, Package.news_id == NewsItem.id)
        .outerjoin(research, research.c.package_id == Package.id)
        .where(or_(in_range, Package.id.is_not(None)))
        .group_by(in_range, state)
    )).all()
    counts = {status: count for selected, status, count in rows if selected}
    outside_counts = {status: count for selected, status, count in rows if not selected}
    outside = {name: outside_counts.get(name, 0) for name in
               ('stale', 'pending', 'processing', 'failed')}
    result = {name: counts.get(name, 0) for name in
              ('missing', 'stale', 'pending', 'processing', 'failed', 'completed')}
    return {'start_at': start_at, 'checked_at': cutoff, 'total': sum(result.values()),
            'unprocessed': sum(result.values()) - result['completed'], **result,
            'outside': {'unprocessed': sum(outside.values()), **outside}}


async def freeze(session, settings):
    cutoff = datetime.now(UTC)
    scope = [NewsItem.published_at >= settings.start_at, NewsItem.published_at <= cutoff]
    realtime_from = getattr(settings, 'realtime_from', None)
    if realtime_from is not None:
        scope.append(NewsItem.published_at < realtime_from)
    candidates = select(NewsItem.id).outerjoin(Package, Package.news_id == NewsItem.id).where(
        *scope, or_(Package.id.is_(None), Package.content_hash != NewsItem.content_hash,
                    Package.state.in_(['index_pending', 'assign_pending'])))
    ids = list(await session.scalars(candidates.order_by(NewsItem.published_at, NewsItem.id).limit(30)))
    await enroll(session, ids)
    existing_ids = list(await session.scalars(select(Package.id).where(Package.news_id.in_(ids))))
    deferred = exists(select(BatchEvent.batch_id).where(BatchEvent.event_id == Event.id,
        BatchEvent.revision == Event.revision, BatchEvent.state == 'deferred'))
    event_query = select(Event.id).where(Event.state == 'pending', scoped_event(settings), ready_event(), Event.due_at <= cutoff, ~deferred)
    if realtime_from is not None:
        event_query = event_query.where(~Event.realtime_pending)
    event_cutoff = list(await session.scalars(event_query.order_by(Event.due_at, Event.id).limit(30)))
    # Failed items are reported once; only explicit retries or new source revisions rerun them.
    previous_failure = exists(select(BatchPackage.batch_id).where(
        BatchPackage.package_id == Package.id, BatchPackage.content_hash == Package.content_hash,
        BatchPackage.state == 'failed'))
    packages = list(await session.scalars(select(Package).where(
        or_(Package.id == any_(existing_ids), Package.news_id == any_(ids)), or_(
        Package.state.in_(['index_pending', 'assign_pending']),
        Package.state.in_(['index_failed', 'assign_failed']) & ~previous_failure))))
    events = list(await session.scalars(select(Event).where(Event.id == any_(event_cutoff), Event.state == 'pending', ~deferred)))
    if not packages and not events:
        return None
    batch = Batch(phase='assigning', cutoff_at=cutoff)
    session.add(batch)
    await session.flush()
    session.add_all(BatchPackage(batch_id=batch.id, package_id=p.id, content_hash=p.content_hash,
        state='failed' if p.state.endswith('_failed') else 'pending') for p in packages)
    event_ids = {e.id for e in events}
    if packages:
        event_ids.update(await session.scalars(select(Membership.event_id).where(
            Membership.package_id == any_([p.id for p in packages]))))
    for e in await session.scalars(select(Event).where(Event.id == any_(list(event_ids)))):
        session.add(BatchEvent(batch_id=batch.id, event_id=e.id, revision=e.revision, state='pending'))
    await session.commit()
    return batch


async def actions(session, batch, active, slots, settings=None):
    """Return stage/ID pairs; callers commit before launching and never overlap phases."""
    if batch.phase == 'assigning':
        rows = list(await session.execute(select(BatchPackage, Package, NewsItem).join(
            Package, Package.id == BatchPackage.package_id).join(NewsItem, NewsItem.id == Package.news_id)
            .where(BatchPackage.batch_id == batch.id).order_by(NewsItem.published_at, NewsItem.id)))
        result = []
        assigning = any(stage == 'assign' for stage, _ in active)
        for item, package, news in rows:
            if item.state != 'pending':
                continue
            if settings is not None and news.published_at < settings.start_at:
                item.state = 'range_paused'
            elif item.content_hash != news.content_hash or item.content_hash != package.content_hash:
                item.state = 'deferred'
            elif package.state == 'done':
                item.state = 'done'
            elif package.state.endswith('_failed'):
                item.state = 'failed'
            elif package.state in ('index_pending', 'assign_pending'):
                stage = 'index' if package.state == 'index_pending' else 'assign'
                if not any(identifier == package.id and task != 'event' for task, identifier in active) and len(result) < slots and (stage != 'assign' or not assigning):
                    result.append((stage, package.id))
                    assigning |= stage == 'assign'
        package_ids = {package.id for _, package, _ in rows}
        batch_active = any(stage != 'event' and identifier in package_ids for stage, identifier in active)
        if any(item.state == 'pending' for item, _, _ in rows) or batch_active:
            return result
        # Memberships now include events created by this batch. Existing affected events
        # were frozen up front, including memberships that have since been removed.
        ids = set(await session.scalars(select(Membership.event_id).join(
            BatchPackage, BatchPackage.package_id == Membership.package_id).where(BatchPackage.batch_id == batch.id)))
        existing = {x.event_id for x in await session.scalars(select(BatchEvent).where(BatchEvent.batch_id == batch.id))}
        for e in await session.scalars(select(Event).where(Event.id == any_(list(ids - existing)))):
            session.add(BatchEvent(batch_id=batch.id, event_id=e.id, revision=e.revision, state='pending'))
        await session.flush()
        latest = select(func.max(Attempt.id)).join(BatchPackage, BatchPackage.package_id == Attempt.package_id).where(
            BatchPackage.batch_id == batch.id, Attempt.stage == 'assign', Attempt.state == 'done').group_by(Attempt.package_id)
        urgent = {int(event_id) for attempt in await session.scalars(select(Attempt).where(Attempt.id.in_(latest)))
                  for event_id, target in attempt.rules.get('membership_targets', {}).items() if target.get('urgent')}
        for item, event in await session.execute(select(BatchEvent, Event).join(Event).where(BatchEvent.batch_id == batch.id)):
            if item.event_id in urgent:
                item.state = 'urgent_pending'
            item.revision = event.revision
        batch.phase = 'researching'
        return []
    result = []
    rows = list(await session.execute(select(BatchEvent, Event).join(Event).where(
        BatchEvent.batch_id == batch.id).order_by((BatchEvent.state == 'urgent_pending').desc(), Event.due_at, Event.id)))
    for item, event in rows:
        if item.state not in ('pending', 'urgent_pending', 'processing') or ('event', event.id) in active:
            continue
        if settings is not None and not await session.scalar(select(Event.id).where(Event.id == event.id, scoped_event(settings))):
            item.state = 'range_paused'
        elif settings is not None and event.realtime_pending:
            item.state = 'realtime'
        elif event.due_at > datetime.now(UTC):
            item.state = 'aggregating'
        elif item.revision != event.revision:
            item.state = 'deferred'
        elif event.state == 'done' or event.state == 'retired':
            item.state = 'done'
        elif event.state == 'failed':
            item.state = 'failed'
        elif item.state == 'processing':
            item.state = 'deferred'  # A completed call was superseded; do not spend again in this batch.
        else:
            invalid = await session.scalar(select(Membership.event_id).join(Package).join(NewsItem).where(
                Membership.event_id == event.id, or_(Package.state != 'done', Package.content_hash != NewsItem.content_hash)).limit(1))
            if invalid:
                item.state = 'blocked'
            elif len(result) < slots:
                item.state = 'processing'
                result.append(('event', event.id))
    event_ids = {event.id for _, event in rows}
    batch_active = any(stage == 'event' and identifier in event_ids for stage, identifier in active)
    active_packages = {identifier for stage, identifier in active if stage != 'event'}
    if active_packages:
        batch_active |= bool(await session.scalar(select(BatchPackage.package_id).where(
            BatchPackage.batch_id == batch.id, BatchPackage.package_id.in_(active_packages)).limit(1)))
    if not result and not batch_active and all(item.state not in ('pending', 'urgent_pending', 'processing') for item, _ in rows):
        bad = any(item.state in ('failed', 'deferred') for item, _ in rows)
        bad |= bool(await session.scalar(select(BatchPackage.batch_id).where(
            BatchPackage.batch_id == batch.id, BatchPackage.state.in_(['failed', 'deferred'])).limit(1)))
        batch.phase = 'partial' if bad else 'done'
        batch.finished_at = datetime.now(UTC)
    return result


async def progress(session):
    batch = await session.scalar(select(Batch).order_by(Batch.id.desc()).limit(1))
    if batch is None:
        return None
    result = {'id': batch.id, 'phase': batch.phase, 'cutoff_at': batch.cutoff_at}
    for name, model in [('packages', BatchPackage), ('events', BatchEvent)]:
        counts = Counter(await session.scalars(select(model.state).where(model.batch_id == batch.id)))
        result.update({name + '_total': sum(counts.values()), **{name + '_' + s: counts[s] for s in ('done', 'failed', 'deferred', 'range_paused', 'blocked', 'realtime')}})
    return result
