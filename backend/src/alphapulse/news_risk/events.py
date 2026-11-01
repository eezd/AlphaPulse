"""Version-bound package relationships and evidence-backed event timelines."""
from datetime import UTC, datetime, timedelta
from pydantic import BaseModel, Field
from sqlalchemy import select
from alphapulse.models.risk import NewsSummary, NewsEvent, EventMember, EventVersion, RiskSettings, EventLink
from alphapulse.core.db import SessionLocal
from alphapulse.news_risk.pipeline import configuration
from alphapulse.news_risk.contracts import PackageTimePoint
from alphapulse.news_risk.service import complete
from alphapulse.news_risk.progress import step


class TimelinePoint(PackageTimePoint):
    package_id: int
    package_revision: int


class EventResult(BaseModel):
    title: str
    summary: str
    category: str
    importance_stars: int = Field(ge=1, le=5)
    risk_stars: int = Field(ge=0, le=5)
    uncertainty: str
    opinions: str
    counter_evidence: str
    watchpoints: str
    timeline_points: list[TimelinePoint] = Field(min_length=1)


def validate_timeline(result, packages):
    versions = {p['id']: p for p in packages}
    if {point.package_id for point in result.timeline_points} != set(versions):
        raise ValueError('事件时间线必须覆盖每个组成包')
    for point in result.timeline_points:
        package = versions.get(point.package_id)
        if package is None or point.package_revision != package['revision']:
            raise ValueError('时间线引用未知包版本')
        allowed = package['result'].get('evidence_refs', []) + [ref for claim in package['result'].get('claims', []) for ref in claim['evidence_refs']]
        allowed += [ref for point in package['result'].get('timeline_points', []) for ref in point['evidence_refs']]
        if point.occurred_at is not None and not any(
            PackageTimePoint.model_validate(p).occurred_at == point.occurred_at and p.get('time_precision') == point.time_precision
            and p.get('time_text') == point.time_text for p in package['result'].get('timeline_points', [])):
            raise ValueError('事件发生时间必须来自组成包已核验时间点')
        if any(ref not in allowed for ref in point.evidence_refs):
            raise ValueError('时间线引用必须来自已核验包的原文证据')
    result.timeline_points.sort(key=lambda p: (p.occurred_at is None, p.occurred_at or datetime.max.replace(tzinfo=UTC), p.package_id))


async def dirty_event(session, package_id, review_config=None):
    membership = await session.get(EventMember, package_id)
    if membership:
        event = await session.get(NewsEvent, membership.event_id, with_for_update=True)
        event.revision += 1
        event.review_config = review_config
        event.state = 'pending'
        event.due_at = datetime.now(UTC)+timedelta(seconds=60)
        if review_config and review_config.get('request'):
            from alphapulse.news_risk.backfill import target
            await target(session, review_config['job_id'], 'events', event)


async def associate(session, package, result, researched_ids, researched_events=None):
    for link in result.related_events:
        if link.id not in (researched_events or set()): raise ValueError('跨事件关联必须引用已读取事件')
        existing = await session.get(EventLink, (package.id, link.id))
        if existing is None:
            session.add(EventLink(package_id=package.id, event_id=link.id, reason=link.reason, package_revision=package.revision))
        else:
            existing.reason = link.reason; existing.package_revision = package.revision
    proposal = result.event_proposal
    if proposal is None:
        await dirty_event(session, package.id, package.review_config)
        return
    ids = sorted(set([package.id, *proposal.package_ids]))
    if len(ids) < 2 or not set(ids)-{package.id} <= researched_ids:
        raise ValueError('事件至少需要两个已查询的不同包')
    members = (await session.scalars(select(NewsSummary).where(NewsSummary.id.in_(ids)).order_by(NewsSummary.id).with_for_update())).all()
    if len(members) != len(ids) or any(p.replacement_ids or p.state == 'withdrawn' for p in members):
        raise ValueError('事件引用失效包')
    existing = (await session.scalars(select(EventMember).where(EventMember.package_id.in_(ids)))).all()
    event_ids = {m.event_id for m in existing}
    if len(event_ids) > 1:
        raise ValueError('多个主事件不能自动合并；请保留跨事件关联')
    if event_ids:
        event = await session.get(NewsEvent, next(iter(event_ids)), with_for_update=True)
        event.revision += 1
    else:
        event = NewsEvent(title=proposal.title, updated_at=package.updated_at, due_at=datetime.now(UTC), state='pending', revision=1)
        session.add(event); await session.flush()
    for identifier in set(ids)-{m.package_id for m in existing}:
        session.add(EventMember(package_id=identifier, event_id=event.id, reason=proposal.reason))
    event.review_config = package.review_config
    event.state = 'pending'; event.due_at = datetime.now(UTC)+timedelta(seconds=60)
    event.updated_at = max(p.updated_at for p in members)
    if package.review_config and package.review_config.get('request'):
        from alphapulse.news_risk.backfill import target
        await target(session, package.review_config['job_id'], 'events', event)


async def organize(target_id=None):
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled: return
        event = await session.scalar(select(NewsEvent).where(NewsEvent.state == 'pending', NewsEvent.due_at <= datetime.now(UTC), NewsEvent.id == target_id if target_id else True).order_by(NewsEvent.due_at).limit(1))
        if event is None: return
        members = (await session.scalars(select(NewsSummary).join(EventMember, EventMember.package_id == NewsSummary.id)
            .where(EventMember.event_id == event.id))).all()
        members = [p for p in members if not p.replacement_ids and p.state != 'withdrawn']
        if len(members) < 2:
            event.state = 'retired'; await session.commit(); return
        if any(p.state != 'done' or p.result is None for p in members):
            if any(p.state == 'failed' for p in members):
                event.state = 'failed'; event.error = '组成包研判失败，请先重试失败包'; await session.commit()
            return
        packages = [{'id': p.id, 'revision': p.published_revision, 'result': p.result, 'published_at': p.published_at.isoformat()} for p in members]
        config_error = None
        if event.review_config:
            from alphapulse.news_risk.reprocess import frozen_configuration
            rules = event.review_config['configurations']['analysis']['rules']
            try:
                provider, system, rules = await frozen_configuration(session, event.review_config, 'analysis')
            except ValueError as error:
                config_error = str(error)
        else:
            provider, system, rules, _ = await configuration(session, settings, 'analysis')
        version = EventVersion(event_id=event.id, revision=event.revision, input={'packages': packages}, rules=rules, state='processing')
        session.add(version); event.state = 'processing'; await session.commit()
        version_id, event_id = version.id, event.id
        try:
            if config_error: raise ValueError(config_error)
            step('梳理事件时间线')
            result, usage = await complete(provider, system, {'task': 'event_timeline', 'previous': event.result, 'packages': packages}, EventResult)
            step('核对时间线与证据')
            validate_timeline(result, packages)
            version.result = result.model_dump(mode='json'); version.usage = usage; version.state = 'done'
            from alphapulse.news_risk.worker import publication_lock
            async with publication_lock:
                current_members = (await session.scalars(select(NewsSummary).join(EventMember, EventMember.package_id == NewsSummary.id)
                    .where(EventMember.event_id == event_id).order_by(NewsSummary.id).with_for_update(of=NewsSummary).execution_options(populate_existing=True))).all()
                current = await session.get(NewsEvent, event_id, populate_existing=True, with_for_update=True)
                current_members = [p for p in current_members if not p.replacement_ids and p.state != 'withdrawn']
                unchanged = {(p.id, p.published_revision) for p in current_members} == {(p['id'], p['revision']) for p in packages}
                unchanged = unchanged and all(p.state == 'done' and not p.replacement_ids for p in current_members)
                if current.revision == version.revision and unchanged:
                    current.published_revision = version.revision
                    current.result = version.result; current.title = result.title; current.state = 'done'; current.error = None
                else:
                    version.state = 'superseded'
                    current.state = 'pending'
                await session.commit()
        except Exception as error:
            await session.rollback()
            version = await session.get(EventVersion, version_id)
            version.state = 'failed'; version.error = str(error)[:2000]
            current = await session.get(NewsEvent, event_id, with_for_update=True)
            current.state = 'pending' if current.revision > version.revision else 'failed'
            current.error = version.error
        await session.commit()


async def reading_inputs(session, cutoff):
    packages = (await session.scalars(select(NewsSummary).where(NewsSummary.result.is_not(None), NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn'))).all()
    memberships = {m.package_id: m.event_id for m in (await session.scalars(select(EventMember))).all()}
    events = {e.id: e for e in (await session.scalars(select(NewsEvent).where(NewsEvent.result.is_not(None), NewsEvent.state != 'retired'))).all()}
    inputs, included = [], set()
    for package in packages:
        if package.updated_at < cutoff: continue
        event = events.get(memberships.get(package.id))
        if event and package.id in {point['package_id'] for point in event.result['timeline_points']}:
            if event.id in included: continue
            included.add(event.id)
            importance = max(event.result['importance_stars'], *(p.importance_stars for p in packages if memberships.get(p.id) == event.id and p.updated_at >= cutoff))
            inputs.append({'kind': 'event', 'id': event.id, 'revision': event.published_revision, 'importance_stars': importance,
                'category': event.result['category'], 'tags': [], 'result': event.result})
        else:
            inputs.append({'kind': 'package', 'id': package.id, 'revision': package.published_revision, 'importance_stars': package.importance_stars,
                'category': package.category, 'tags': package.tags, 'result': package.result})
    return inputs
