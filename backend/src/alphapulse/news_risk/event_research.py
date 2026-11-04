"""Event-scoped research from complete member packages and deduplicated originals."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, exists
from alphapulse.core.db import SessionLocal
from alphapulse.models import NewsItem
from alphapulse.models.risk import (NewsEvent, NewsSummary, EventMember, EventVersion, EventEvidence,
    SummaryArticle, ArticleAnalysis, RiskSettings, EventCorrectionRecord)
from alphapulse.news_risk.pipeline import ReviewResult, configuration, validate_event
from alphapulse.news_risk.events import TimelinePoint
from alphapulse.news_risk.research import Research
from alphapulse.news_risk.service import SubmissionFailure, accumulate_usage
from alphapulse.news_risk.pipeline import extract_articles, compact_extractions
from alphapulse.news_risk.contracts import Correction


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0)
    revision: int = Field(ge=0)


class EventReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_id: int | None = Field(gt=0)
    title: str = Field(min_length=1)
    fact: str = Field(min_length=1)
    package_ids: list[int] = Field(min_length=1)
    reason: str = Field(min_length=1)


class EventCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[EventInput] = Field(min_length=1)
    replacements: list[EventReplacement] = Field(min_length=1)
    reason: str = Field(min_length=1)


class EventResult(ReviewResult):
    package_correction: Correction | None = None
    timeline_points: list[TimelinePoint] = Field(min_length=1)
    correction: EventCorrection | None = None


def schedule(event, now, urgent=False):
    ready = event.revision > 0 and event.state in ('pending','processing') and event.due_at <= now
    event.revision += 1
    event.dirty_since = event.dirty_since or now
    event.due_at = now if urgent or ready else min(now+timedelta(minutes=5), event.dirty_since+timedelta(minutes=15))
    event.state = 'pending'
    event.error = None


async def attach(session, package, target, groups, changed, urgent, frozen):
    membership = await session.get(EventMember, package.id)
    if membership:
        if target.event_id != membership.event_id:
            raise ValueError('已有包必须沿用主事件；事件调整须通过版本校验纠错')
        event = await session.get(NewsEvent, membership.event_id, with_for_update=True, populate_existing=True)
        if event is None or event.state == 'retired':
            raise ValueError('所属事件已失效')
    else:
        event = await session.get(NewsEvent, target.event_id, with_for_update=True, populate_existing=True) if target.event_id else groups.get(target.group_key)
        if target.event_id and (event is None or event.state == 'retired'):
            raise ValueError('候选事件已失效')
        if event is None:
            event = NewsEvent(title=target.title, fact=target.fact, category=package.category, tags=[],
                revision=0, updated_at=package.updated_at, due_at=datetime.now(UTC), state='pending')
            session.add(event)
            await session.flush()
            groups[target.group_key] = event
        session.add(EventMember(package_id=package.id, event_id=event.id, reason=target.reason))
        changed = True
    event.tags = sorted(set(event.tags or []) | set(package.tags))
    if changed:
        event.updated_at = max(event.updated_at, package.updated_at)
        event.review_config = frozen
        schedule(event, datetime.now(UTC), urgent)
        if frozen and frozen.get('request'):
            from alphapulse.news_risk.backfill import target as enroll
            await enroll(session, frozen['job_id'], 'events', event)


async def members(session, identifier):
    return list(await session.scalars(select(NewsSummary).join(EventMember).where(EventMember.event_id == identifier,
        NewsSummary.replacement_ids == [], NewsSummary.state != 'withdrawn').order_by(NewsSummary.id).execution_options(populate_existing=True)))


async def originals_ready(session, package_ids):
    referenced = select(ArticleAnalysis.news_id).join(SummaryArticle).where(
        SummaryArticle.summary_id.in_(package_ids), SummaryArticle.active.is_(True))
    ready = exists(select(ArticleAnalysis.id).where(ArticleAnalysis.news_id==NewsItem.id,
        ArticleAnalysis.content_hash==NewsItem.content_hash, ArticleAnalysis.state=='done'))
    return await session.scalar(select(NewsItem.id).where(NewsItem.id.in_(referenced),~ready).limit(1)) is None


async def correct(session, proposal, current_id, research, rules):
    ids = [e.id for e in proposal.events]
    if len(ids) != len(set(ids)) or current_id not in ids or not set(ids)-{current_id} <= research.read_event_ids:
        raise ValueError('纠错只能操作当前事件及实际读取的事件')
    if any(e.id != current_id and research.read_event_versions.get(e.id) != e.revision for e in proposal.events):
        raise ValueError('纠错版本必须来自实际读取的事件')
    events = list(await session.scalars(select(NewsEvent).where(NewsEvent.id.in_(ids)).order_by(NewsEvent.id).with_for_update()))
    versions = {e.id:e.revision for e in proposal.events}
    if len(events) != len(ids) or any(e.state == 'retired' or e.revision != versions[e.id] for e in events):
        raise ValueError('事件纠错输入已变化')
    links = list(await session.scalars(select(EventMember).join(NewsSummary).where(EventMember.event_id.in_(ids),
        NewsSummary.replacement_ids==[], NewsSummary.state!='withdrawn').with_for_update(of=EventMember)))
    assigned = [p for r in proposal.replacements for p in r.package_ids]
    if len(assigned) != len(set(assigned)) or set(assigned) != {l.package_id for l in links}:
        raise ValueError('事件纠错必须完整且唯一分配所有成员包')
    retained = [r.keep_id for r in proposal.replacements if r.keep_id is not None]
    if len(retained) != len(set(retained)) or not set(retained) <= set(ids):
        raise ValueError('纠错保留事件无效')
    if len(ids)>1 and len(proposal.replacements)==1 and retained != [min(ids)]:
        raise ValueError('合并保留最早事件ID')
    if not await originals_ready(session,assigned): raise ValueError('事件原文已修订，请先完成重新归组')
    source_keys = list(await session.execute(select(ArticleAnalysis.news_id, ArticleAnalysis.content_hash).join(SummaryArticle).join(NewsItem).where(
        SummaryArticle.summary_id.in_(assigned),SummaryArticle.active.is_(True),ArticleAnalysis.content_hash==NewsItem.content_hash).distinct()))
    key = hashlib.sha256(json.dumps([sorted(tuple(r) for r in source_keys),rules],sort_keys=True).encode()).hexdigest()
    if await session.scalar(select(EventCorrectionRecord.id).where(EventCorrectionRecord.fingerprint==key)):
        raise ValueError('相同证据已纠正事件，拒绝反复拆合')
    session.add(EventCorrectionRecord(fingerprint=key,input={'events':[e.model_dump() for e in proposal.events]},result=proposal.model_dump(mode='json')))
    for event in events:
        if event.id not in retained: event.state = 'retired'
    for replacement in proposal.replacements:
        event = next((e for e in events if e.id==replacement.keep_id), None)
        if event is None:
            event = NewsEvent(title=replacement.title, fact=replacement.fact, revision=0, updated_at=datetime.now(UTC), due_at=datetime.now(UTC))
            session.add(event); await session.flush()
        packages = list(await session.scalars(select(NewsSummary).where(NewsSummary.id.in_(replacement.package_ids))))
        event.tags = sorted({t for p in packages for t in p.tags})
        event.category = packages[0].category
        event.updated_at = max(p.updated_at for p in packages)
        event.title = replacement.title; event.fact = replacement.fact
        event.review_config = next(e.review_config for e in events if e.id==current_id)
        for link in links:
            if link.package_id in replacement.package_ids:
                link.event_id = event.id; link.reason = replacement.reason
        schedule(event, datetime.now(UTC), True)
        if event.review_config and event.review_config.get('request'):
            from alphapulse.news_risk.backfill import target
            await target(session, event.review_config['job_id'], 'events', event)


def validate(result, packages, sources):
    validate_event(result, sources)
    expected = {p['id']: p for p in packages}
    if {p.package_id for p in result.timeline_points} != set(expected):
        raise ValueError('事件时间线必须覆盖全部成员包')
    for point in result.timeline_points:
        package = expected.get(point.package_id)
        if package is None or point.package_revision != package['revision']:
            raise ValueError('时间线引用未知包版本')
        if not any(ref['article_id'] in package['article_ids'] for ref in point.evidence_refs):
            raise ValueError('时间线缺少对应成员包原文依据')
    result.timeline_points.sort(key=lambda p:(p.occurred_at is None,p.occurred_at or p.planned_at or p.reported_at or datetime.max.replace(tzinfo=UTC),p.package_id))


async def generate(provider, system, payload, research):
    parts, used = [], None
    originals = payload['articles']
    try:
        if not research.fits(provider, system, payload, EventResult):
            extracts, parts = await extract_articles(provider, system, originals)
            if {ref['article_id'] for e in extracts for ref in e['evidence_refs']} != {a['id'] for a in originals}:
                raise ValueError('分层提取遗漏事件原文')
            payload = {k:v for k,v in payload.items() if k!='articles'}
            payload['extractions'] = extracts
            payload, compact = await compact_extractions(provider,system,payload,EventResult,research.fits)
            parts.extend(compact)
        result, used = await research.run(provider,system,payload,EventResult)
        return result, {'parts':parts,'final':used}
    except Exception as error:
        raise SubmissionFailure(str(error), {'parts':parts,'final':used or getattr(error,'usage',None) or {'rounds':getattr(research,'usages',[])}}) from error


async def review(identifier):
    from alphapulse.news_risk.worker import publication_lock
    async with SessionLocal() as session:
        settings = await session.get(RiskSettings, 1)
        if not settings or not settings.enabled: return
        event = await session.get(NewsEvent, identifier, with_for_update=True)
        if event is None or event.state!='pending' or event.due_at>datetime.now(UTC): return
        packages = await members(session, identifier)
        if not packages:
            event.state='retired'; await session.commit(); return
        articles = {}
        source_error = None if await originals_ready(session,[p.id for p in packages]) else '事件原文已修订，请先完成重新归组'
        inputs = []
        for package in packages:
            rows = list(await session.scalars(select(ArticleAnalysis).join(SummaryArticle).join(NewsItem).where(
                SummaryArticle.summary_id==package.id, SummaryArticle.active.is_(True),
                ArticleAnalysis.content_hash==NewsItem.content_hash, ArticleAnalysis.state=='done')))
            if not rows:
                source_error='成员包缺少有效原文，请先完成原文归组'
            for row in rows: articles[row.id] = {'id':row.id,'news_id':row.news_id,'content_hash':row.content_hash,**row.input}
            inputs.append({'id':package.id,'revision':package.revision,'title':package.title,'fact':package.fact,
                'tags':package.tags,'published_at':package.published_at.isoformat(),'article_ids':[r.id for r in rows]})
        config_error = source_error
        if event.review_config:
            from alphapulse.news_risk.reprocess import frozen_configuration
            rules = event.review_config['configurations']['analysis']['rules']
            try: provider, system, rules = await frozen_configuration(session,event.review_config,'analysis')
            except ValueError as error: provider=None; system=''; config_error=str(error)
        else:
            try: provider, system, rules, _ = await configuration(session,settings,'analysis')
            except ValueError as error: provider=None; system=''; rules={}; config_error=str(error)
        payload = {'task':'event_research','id':event.id,'event':{'id':event.id,'revision':event.revision,
            'title':event.title,'fact':event.fact,'previous':event.result},'packages':inputs,'articles':list(articles.values())}
        version = await session.scalar(select(EventVersion).where(EventVersion.event_id==identifier, EventVersion.revision==event.revision, EventVersion.rules==rules).order_by(EventVersion.id.desc()).limit(1))
        if version is None or version.state != 'failed':
            version = EventVersion(event_id=event.id,revision=event.revision,input=payload,rules=rules,state='processing')
            session.add(version)
        else: version.state='processing'; version.error=None; version.input=payload
        previous_usage = version.usage
        event.state='processing'; await session.commit()
        version_id, revision = version.id, version.revision
        research = Research(event={'id':identifier,'revision':revision})
        records = list(await session.scalars(select(EventEvidence).where(EventEvidence.version_id==version_id)))
        research.restore([e.data for e in records])
        usage = None
        try:
            if config_error: raise ValueError(config_error)
            result, usage = await generate(provider,system,payload,research)
            if result.id != identifier: raise ValueError('事件ID不匹配')
            if result.correction is not None and result.package_correction is not None: raise ValueError('一次只能进行事件纠错或分包纠错')
            if result.correction is None and result.package_correction is None: validate(result,inputs,{**articles,**research.sources})
            async with publication_lock:
                current = await session.get(NewsEvent,identifier,populate_existing=True,with_for_update=True)
                current_members = await members(session,identifier)
                unchanged = current.revision==revision and {(p.id,p.revision) for p in current_members}=={(p['id'],p['revision']) for p in inputs}
                hashes = list(await session.execute(select(ArticleAnalysis.id,NewsItem.content_hash).join(NewsItem)
                    .where(ArticleAnalysis.id.in_(articles))))
                unchanged = unchanged and all(articles[i]['content_hash']==h for i,h in hashes) and len(hashes)==len(articles)
                unchanged = unchanged and await originals_ready(session,[p.id for p in current_members])
                version.result=result.model_dump(mode='json'); version.usage=accumulate_usage(previous_usage,usage)
                if not unchanged:
                    version.state='superseded'
                    if current.state!='retired': current.state='pending'
                elif result.package_correction:
                    from alphapulse.news_risk.corrections import apply_correction
                    proposal = result.package_correction
                    own_ids = {p['id'] for p in inputs}
                    anchors = own_ids & {p.id for p in proposal.packages}
                    if not anchors: raise ValueError('分包纠错必须包含当前事件的成员包')
                    await apply_correction(session,proposal,min(anchors),research.read_package_ids | own_ids,rules)
                    version.state='corrected'
                elif result.correction:
                    await correct(session,result.correction,identifier,research,rules)
                    version.state='corrected'
                else:
                    current.result=version.result; current.title=result.title; current.category=result.category
                    current.published_revision=revision; current.state='done'; current.error=None; current.dirty_since=None
                    version.state='done'
        except Exception as error:
            await session.rollback()
            version=await session.get(EventVersion,version_id)
            version.state='failed'; version.error=str(error)[:2000]
            version.usage=accumulate_usage(previous_usage,getattr(error,'usage',None) or usage or {'rounds':getattr(research,'usages',[])})
            current=await session.get(NewsEvent,identifier,with_for_update=True)
            if current.state!='retired': current.state='pending' if current.revision>revision else 'failed'
            current.error=version.error
        for record in research.records: session.add(EventEvidence(version_id=version_id,data=record))
        await session.commit()
