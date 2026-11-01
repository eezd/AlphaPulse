"""Apply version-checked, reversible package corrections as one transaction."""
import hashlib
import json
from datetime import UTC, datetime
from sqlalchemy import select
from alphapulse.models import NewsItem
from alphapulse.models.risk import NewsSummary, SummaryArticle, ArticleAnalysis, PackageCorrection, EventMember
from alphapulse.news_risk.pipeline import Evidence, validate_evidence
from alphapulse.news_risk.contracts import Correction


async def apply_correction(session, proposal: Correction, current_id, researched_ids, rules):
    from alphapulse.news_risk.worker import schedule
    from alphapulse.news_risk.events import dirty_event
    ids = [p.id for p in proposal.packages]
    if len(ids) != len(set(ids)) or current_id not in ids or not set(ids)-{current_id} <= researched_ids:
        raise ValueError('纠错必须包含当前包，且只能操作已读取的其他包')
    packages = (await session.scalars(select(NewsSummary).where(NewsSummary.id.in_(ids)).order_by(NewsSummary.id).with_for_update())).all()
    expected = {p.id: p.revision for p in proposal.packages}
    if len(packages) != len(ids) or any(p.revision != expected[p.id] or p.replacement_ids for p in packages):
        raise ValueError('纠错输入版本已变化')
    links = (await session.scalars(select(SummaryArticle).where(SummaryArticle.summary_id.in_(ids), SummaryArticle.active.is_(True)))).all()
    sources = {}
    for link in links:
        article = await session.get(ArticleAnalysis, link.analysis_id)
        news = await session.get(NewsItem, article.news_id)
        if article.content_hash == news.content_hash:
            sources[article.id] = article
    assigned = {a.analysis_id for r in proposal.replacements for a in r.assignments}
    if assigned != set(sources): raise ValueError('纠错遗漏来源或引入未知文章')
    retained = [r.keep_id for r in proposal.replacements if r.keep_id is not None]
    if len(set(retained)) != len(retained) or not set(retained) <= set(ids): raise ValueError('纠错保留包ID无效')
    if len(ids) > 1 and len(proposal.replacements) == 1 and retained != [min(ids)]:
        raise ValueError('合包必须保留最早包ID')
    for replacement in proposal.replacements:
        if len({a.analysis_id for a in replacement.assignments}) != len(replacement.assignments): raise ValueError('重复的原文关联')
        for assignment in replacement.assignments:
            validate_evidence([Evidence(article_id=assignment.analysis_id, quote=assignment.evidence)], {i: a.input for i,a in sources.items()})
    # The source set and rules, rather than mutable revision numbers, bound retries.
    fingerprint = hashlib.sha256(json.dumps([sorted((a.news_id,a.content_hash) for a in sources.values()), rules], sort_keys=True).encode()).hexdigest()
    if await session.scalar(select(PackageCorrection.id).where(PackageCorrection.fingerprint == fingerprint)):
        raise ValueError('相同输入已纠错，拒绝反复拆合；需要新的证据')
    before = {'packages': [{'id':p.id,'revision':p.revision,'fact':p.fact,'tags':p.tags} for p in packages],
        'memberships': [{'package_id':l.summary_id,'analysis_id':l.analysis_id,'unit':l.unit} for l in links]}
    for package in packages: await dirty_event(session, package.id, next(p.review_config for p in packages if p.id == current_id))
    for link in links: link.active = False
    outputs = []
    now = datetime.now(UTC)
    for replacement in proposal.replacements:
        package = next((p for p in packages if p.id == replacement.keep_id), None)
        dates = [datetime.fromisoformat(sources[a.analysis_id].input['published_at']) for a in replacement.assignments]
        if package is None:
            package = NewsSummary(title=replacement.title, fact=replacement.fact, category=replacement.category, tags=replacement.tags,
                entities=[], content_kind=replacement.content_kind, published_at=min(dates), updated_at=max(dates), changed_at=now, due_at=now,
                revision=0, published_revision=0, state='pending')
            session.add(package); await session.flush()
        package.title = replacement.title; package.fact = replacement.fact; package.category = replacement.category
        package.tags = sorted({t.strip().casefold() for t in replacement.tags if t.strip()})
        if not package.tags: raise ValueError('纠错标签不能为空')
        package.published_at = min(dates)
        package.updated_at = max(datetime.fromisoformat(sources[a.analysis_id].input['progress_at']) for a in replacement.assignments)
        package.review_config = next(p.review_config for p in packages if p.id == current_id)
        package.content_kind = replacement.content_kind
        for assignment in replacement.assignments:
            link = next((l for l in links if l.summary_id == package.id and l.analysis_id == assignment.analysis_id), None)
            if link is None:
                link = SummaryArticle(summary_id=package.id, analysis_id=assignment.analysis_id); session.add(link)
            link.active = True; link.unit = {'fact':replacement.fact,'evidence':assignment.evidence,'correction':True}
        schedule(package, now, True); outputs.append(package.id)
        if package.review_config and package.review_config.get('request'):
            from alphapulse.news_risk.backfill import target
            await target(session, package.review_config['job_id'], 'analysis', package)
    for package in packages:
        if package.id not in outputs:
            package.replacement_ids = outputs; package.state = 'superseded'
            membership = await session.get(EventMember, package.id)
            if membership: await session.delete(membership)
    session.add(PackageCorrection(fingerprint=fingerprint, input=before, result={'proposal':proposal.model_dump(),'package_ids':outputs}))
    return outputs
