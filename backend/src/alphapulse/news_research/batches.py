"""Durable admission barriers: finish this batch's assignments before any research."""
from collections import Counter
from datetime import UTC, datetime
from sqlalchemy import select, or_, exists, func
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Batch, BatchPackage, BatchEvent, Package, Event, Membership, Attempt
from alphapulse.news_research.service import enroll


async def freeze(session, settings):
    cutoff = datetime.now(UTC)
    ids = list(await session.scalars(select(NewsItem.id).outerjoin(Package, Package.news_id == NewsItem.id).where(
        NewsItem.published_at >= settings.start_at, NewsItem.published_at <= cutoff,
        or_(Package.id.is_(None), Package.content_hash != NewsItem.content_hash))))
    existing_ids = list(await session.scalars(select(Package.id).where(
        Package.state.in_(['index_pending', 'assign_pending', 'index_failed', 'assign_failed']))))
    event_cutoff = list(await session.scalars(select(Event.id).where(Event.state == "pending")))
    await enroll(session, ids)
    # Failed items are reported once; only explicit retries or new source revisions rerun them.
    previous_failure = exists(select(BatchPackage.batch_id).where(
        BatchPackage.package_id == Package.id, BatchPackage.content_hash == Package.content_hash,
        BatchPackage.state == 'failed'))
    packages = list(await session.scalars(select(Package).where(
        or_(Package.id.in_(existing_ids), Package.news_id.in_(ids)), or_(
        Package.state.in_(['index_pending', 'assign_pending']),
        Package.state.in_(['index_failed', 'assign_failed']) & ~previous_failure))))
    deferred = exists(select(BatchEvent.batch_id).where(BatchEvent.event_id == Event.id,
        BatchEvent.revision == Event.revision, BatchEvent.state == 'deferred'))
    events = list(await session.scalars(select(Event).where(Event.id.in_(event_cutoff), Event.state == 'pending', ~deferred)))
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
            Membership.package_id.in_([p.id for p in packages]))))
    for e in await session.scalars(select(Event).where(Event.id.in_(event_ids))):
        session.add(BatchEvent(batch_id=batch.id, event_id=e.id, revision=e.revision, state='pending'))
    await session.commit()
    return batch


async def actions(session, batch, active, slots):
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
            if item.content_hash != news.content_hash or item.content_hash != package.content_hash:
                item.state = 'deferred'
            elif package.state == 'done':
                item.state = 'done'
            elif package.state.endswith('_failed'):
                item.state = 'failed'
            elif package.state in ('index_pending', 'assign_pending'):
                stage = 'index' if package.state == 'index_pending' else 'assign'
                if (stage, package.id) not in active and len(result) < slots and (stage != 'assign' or not assigning):
                    result.append((stage, package.id))
                    assigning |= stage == 'assign'
        if any(item.state == 'pending' for item, _, _ in rows) or active:
            return result
        # Memberships now include events created by this batch. Existing affected events
        # were frozen up front, including memberships that have since been removed.
        ids = set(await session.scalars(select(Membership.event_id).join(
            BatchPackage, BatchPackage.package_id == Membership.package_id).where(BatchPackage.batch_id == batch.id)))
        existing = {x.event_id for x in await session.scalars(select(BatchEvent).where(BatchEvent.batch_id == batch.id))}
        for e in await session.scalars(select(Event).where(Event.id.in_(ids - existing))):
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
        if item.revision != event.revision:
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
                item.state = 'deferred'
            elif len(result) < slots:
                item.state = 'processing'
                result.append(('event', event.id))
    if not result and not active and all(item.state not in ('pending', 'urgent_pending', 'processing') for item, _ in rows):
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
        result.update({name + '_total': sum(counts.values()), **{name + '_' + s: counts[s] for s in ('done', 'failed', 'deferred')}})
    return result
