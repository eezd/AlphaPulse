"""Priority selection over durable package and event states; no paid work here."""
from datetime import UTC, datetime
from sqlalchemy import select, exists, or_, and_, func, case
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Package, Event, Membership, Attempt, Version, ResearchSettings


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


async def realtime_actions(session, settings, active, slots):
    if settings.realtime_from is None or slots <= 0:
        return []
    now = datetime.now(UTC)
    busy_packages = {identifier for stage, identifier in active if stage != 'event'}
    busy_events = {identifier for stage, identifier in active if stage == 'event'}
    result = []
    events = await session.scalars(select(Event).where(
        Event.state == 'pending', Event.realtime_pending, Event.due_at <= now,
        scoped_event(settings), ready_event(), Event.id.not_in(busy_events),
        ~exists(select(Version.id).where(Version.event_id == Event.id, Version.state == 'processing')),
    ).order_by(Event.due_at, Event.id).limit(slots))
    result.extend(('event', event.id) for event in events)
    # Known historical dependencies inherit the waiting realtime event's priority.
    dependency = exists(select(Membership.package_id).join(Event).where(
        Membership.package_id == Package.id, Event.realtime_pending, Event.state == 'pending'))
    priority = or_(NewsItem.published_at >= settings.realtime_from, dependency)
    candidates = select(NewsItem.id).outerjoin(Package, Package.news_id == NewsItem.id).where(
        eligible(settings), priority, or_(Package.id.is_(None), Package.content_hash != NewsItem.content_hash),
        or_(Package.id.is_(None), Package.id.not_in(busy_packages))).order_by(NewsItem.published_at, NewsItem.id).limit(30)
    ids = list(await session.scalars(candidates))
    if ids:
        from alphapulse.news_research.service import enroll
        await enroll(session, ids)
    assigning = any(stage == 'assign' for stage, _ in active)
    for stage in ('assign', 'index'):
        if stage == 'assign' and assigning:
            continue
        remaining = slots - len(result)
        if remaining <= 0:
            break
        query = select(Package.id).join(NewsItem).where(
            eligible(settings), priority, Package.state == stage + '_pending',
            Package.id.not_in(busy_packages),
            ~exists(select(Attempt.id).where(Attempt.package_id == Package.id, Attempt.state == 'processing')),
        ).order_by(NewsItem.published_at, NewsItem.id).limit(1 if stage == 'assign' else remaining)
        result.extend((stage, identifier) for identifier in await session.scalars(query))
    return result


async def queue_status(session, settings):
    running = await session.scalar(select(func.count()).select_from(Attempt).where(Attempt.state == 'processing'))
    running += await session.scalar(select(func.count()).select_from(Version).where(Version.state == 'processing'))
    realtime_from = settings.realtime_from
    lane = case((NewsItem.published_at >= realtime_from, 'realtime'), else_='backfill') if realtime_from else 'backfill'
    if realtime_from:
        groups = (await session.execute(select(lane, Package.state, func.count()).join(NewsItem)
                   .where(eligible(settings)).group_by(lane, Package.state))).all()
    else:
        groups = [('backfill', state, count) for state, count in (await session.execute(
            select(Package.state, func.count()).join(NewsItem).where(eligible(settings)).group_by(Package.state))).all()]
    queues = {name: {state: count for selected, state, count in groups if selected == name}
              for name in ('realtime', 'backfill')}
    return {'running': running, 'draining': not settings.enabled and running > 0, **queues}
