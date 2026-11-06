"""Priority selection over durable package and event states; no paid work here."""
from datetime import UTC, datetime
from sqlalchemy import select, exists, or_, and_, func, case, literal
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Package, Event, Membership, Attempt, Version, ResearchSettings, BatchEvent


def boundary(settings):
    realtime_from = getattr(settings, 'realtime_from', None)
    return min(settings.start_at, realtime_from) if realtime_from else settings.start_at


def eligible(settings):
    return and_(NewsItem.published_at >= boundary(settings), NewsItem.published_at <= datetime.now(UTC))


def members(event_id):
    return select(Membership.package_id).where(Membership.event_id == event_id)


def ready_event():
    return ~exists(select(Membership.package_id).join(Package).join(NewsItem).where(
        Membership.event_id == Event.id,
        or_(Package.state != 'done', Package.content_hash != NewsItem.content_hash)))


def scoped_event(settings):
    # Empty events must still reach the existing no-model retirement path.
    return or_(exists(select(Membership.package_id).join(Package).join(NewsItem).where(
        Membership.event_id == Event.id, eligible(settings))),
        ~exists(select(Membership.package_id).where(Membership.event_id == Event.id)))


async def control(session, config):
    """The control lock spans the durable claim, never the remote model call."""
    if '_control_revision' not in config:
        return None  # Explicit bounded/offline callers retain their existing contract.
    row = await session.get(ResearchSettings, 1, populate_existing=True, with_for_update=True)
    if not row.enabled or row.control_revision != config['_control_revision']:
        return False
    return row


async def mark_priority(session, event, news):
    settings = await session.get(ResearchSettings, 1)
    if settings and settings.realtime_from and news.published_at >= settings.realtime_from:
        event.realtime_pending = True


def manual_review():
    """Legacy deferred revisions and rejected current snapshots require explicit retry."""
    return or_(
        exists(select(BatchEvent.event_id).where(BatchEvent.event_id == Event.id,
            BatchEvent.revision == Event.revision, BatchEvent.state == 'deferred')),
        exists(select(Version.id).where(Version.event_id == Event.id,
            Version.revision == Event.revision, Version.state == 'superseded')),
    )


def priority_package(settings):
    if settings.realtime_from is None:
        return literal(False)
    dependency = exists(select(Membership.package_id).join(Event).where(
        Membership.package_id == Package.id, Event.realtime_pending, Event.state == 'pending'))
    return or_(NewsItem.published_at >= settings.realtime_from, dependency)


def active_research():
    return exists(select(Version.id).where(Version.event_id == Event.id, Version.state == 'processing'))


def runnable_event(settings):
    return and_(Event.state == 'pending', scoped_event(settings), ready_event(),
        Event.due_at <= datetime.now(UTC), ~manual_review(),
        ~active_research())


async def lane_actions(session, settings, active, slots, realtime):
    if slots <= 0 or (realtime and settings.realtime_from is None):
        return []
    busy_packages = {identifier for stage, identifier in active if stage != 'event'}
    busy_events = {identifier for stage, identifier in active if stage == 'event'}
    scope = priority_package(settings) if realtime else ~priority_package(settings)
    result = []
    # The chronological head includes missing and still-indexing originals. Failed
    # source revisions are skipped, but changed originals become eligible again.
    if not any(stage == 'assign' for stage, _ in active):
        head = (await session.execute(select(Package.id, Package.state, Package.content_hash,
            NewsItem.content_hash).select_from(NewsItem).outerjoin(Package, Package.news_id == NewsItem.id)
            .where(eligible(settings), scope, or_(Package.id.is_(None),
                Package.content_hash != NewsItem.content_hash,
                Package.state.in_(['index_pending', 'index_processing', 'assign_pending', 'assign_processing'])))
            .order_by(NewsItem.published_at, NewsItem.id).limit(1))).first()
        if head and head[1] == 'assign_pending' and head[2] == head[3] and head[0] not in busy_packages:
            if not await session.scalar(select(Attempt.id).where(
                    Attempt.package_id == head[0], Attempt.state == 'processing').limit(1)):
                result.append(('assign', head[0]))
    events = await session.scalars(select(Event.id).where(
        runnable_event(settings), Event.realtime_pending == realtime,
        Event.id.not_in(busy_events)).order_by(Event.due_at, Event.id).limit(slots - len(result)))
    result.extend(('event', identifier) for identifier in events)
    remaining = slots - len(result)
    if remaining > 0:
        ids = list(await session.scalars(select(NewsItem.id).outerjoin(Package, Package.news_id == NewsItem.id)
            .where(eligible(settings), scope,
                or_(Package.id.is_(None), Package.content_hash != NewsItem.content_hash, Package.state == 'index_pending'),
                or_(Package.id.is_(None), Package.id.not_in(busy_packages)),
                ~exists(select(Attempt.id).where(Attempt.package_id == Package.id, Attempt.state == 'processing')))
            .order_by(NewsItem.published_at, NewsItem.id).limit(remaining)))
        if ids:
            from alphapulse.news_research.service import enroll
            await enroll(session, ids)
            result.extend(('index', identifier) for identifier in await session.scalars(
                select(Package.id).join(NewsItem).where(Package.news_id.in_(ids), Package.state == 'index_pending')
                .order_by(NewsItem.published_at, NewsItem.id)))
    return result


async def realtime_actions(session, settings, active, slots):
    return await lane_actions(session, settings, active, slots, True)


async def actions(session, settings, active, slots):
    result = await realtime_actions(session, settings, active, slots)
    result += await lane_actions(session, settings, active | set(result), slots - len(result), False)
    return result


async def queue_status(session, settings):
    stages = dict((await session.execute(select(Attempt.stage, func.count()).where(
        Attempt.state == 'processing').group_by(Attempt.stage))).all())
    stages['research'] = await session.scalar(select(func.count()).select_from(Version).where(Version.state == 'processing'))
    stages = {stage: stages.get(stage, 0) for stage in ('index', 'assign', 'research')}
    running = sum(stages.values())
    capacity = settings.config.get('concurrency', 3)
    lane = case((priority_package(settings), 'realtime'), else_='backfill')
    state = case((Package.id.is_(None), 'missing'),
        (Package.content_hash != NewsItem.content_hash, 'stale'), else_=Package.state)
    groups = (await session.execute(select(lane, state, func.count()).select_from(NewsItem)
        .outerjoin(Package, Package.news_id == NewsItem.id).where(eligible(settings)).group_by(lane, state))).all()
    queues = {name: {status: count for selected, status, count in groups if selected == name}
              for name in ('realtime', 'backfill')}
    event_state = case((Event.state == 'processing', 'processing'),
        (active_research(), 'waiting_previous'), (Event.state == 'failed', 'failed'),
        (manual_review(), 'manual_review'), (~ready_event(), 'blocked'),
        (Event.due_at > datetime.now(UTC), 'aggregating'), else_='ready')
    event_groups = (await session.execute(select(Event.realtime_pending, event_state, func.count()).where(
        Event.state.in_(['pending', 'processing', 'failed']), scoped_event(settings))
        .group_by(Event.realtime_pending, event_state))).all()
    events = {name: {status: count for realtime, status, count in event_groups if realtime == (name == 'realtime')}
              for name in ('realtime', 'backfill')}
    reasons = []
    if running < capacity:
        if not settings.enabled:
            reasons.append('draining' if running else 'paused')
        else:
            if any(q.get('assign_pending', 0) for q in queues.values()):
                reasons.append('assignment_serial' if stages['assign'] else 'preceding_index')
            for status, reason in [('waiting_previous', 'waiting_previous'), ('aggregating', 'aggregation'), ('blocked', 'dependencies'),
                                   ('manual_review', 'manual_retry'), ('failed', 'manual_retry')]:
                if any(q.get(status, 0) for q in events.values()) and reason not in reasons:
                    reasons.append(reason)
            if any(count for q in queues.values() for state, count in q.items() if state.endswith('_failed')) and 'manual_retry' not in reasons:
                reasons.append('manual_retry')
            if not reasons:
                reasons.append('no_work' if not any(
                    q.get(s, 0) for q in queues.values() for s in ('missing', 'stale', 'index_pending', 'assign_pending')
                ) and not any(q.get('ready', 0) for q in events.values()) else 'dispatching')
    return {'running': running, 'capacity': capacity, 'running_by_stage': stages,
            'draining': not settings.enabled and running > 0, 'events': events, 'idle_reasons': reasons, **queues}
