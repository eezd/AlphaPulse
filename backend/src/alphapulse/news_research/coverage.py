"""Coverage reporting over durable news and research states."""
from datetime import UTC, datetime
from sqlalchemy import select, or_, and_, func, case
from alphapulse.models import NewsItem
from alphapulse.news_research.models import Package, Event, Membership


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
