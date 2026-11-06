"""Persistent keyword indexing, membership and versioned event research."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from sqlalchemy import select, update, func, or_, case, column, exists, true, literal, any_
from sqlalchemy.dialects.postgresql import insert
from alphapulse.core.db import SessionLocal
from alphapulse.models import NewsItem
from alphapulse.news_research.model import complete, provider_for
from alphapulse.news_research.pipeline import (
    validate_evidence,
    Evidence,
    validate_event,
    align_quotes,
    extract_articles,
    compact_extractions,
)
from alphapulse.news_research.tools import Research as ResearchTools
from alphapulse.news_research.models import (
    Package,
    Attempt,
    Event,
    Membership,
    Version,
    Reference,
)
from alphapulse.news_research.contracts import (
    INDEX_ASSIGN_SKILL,
    task_skill,
    RESEARCH_SKILL,
    IndexResult,
    Assignment,
    ResearchResult,
)

from alphapulse.news_research import vocabulary as vocab

publication_lock = asyncio.Lock()
assignment_lock = asyncio.Lock()


def article(news):
    return {
        "id": news.id,
        "news_id": news.id,
        "title": news.title,
        "summary": news.summary,
        "content": news.content_text,
        "url": news.url,
        "published_at": news.published_at.isoformat(),
    }


def validate_keywords(result, source):
    seen = set()
    for keyword in result.keywords:
        key = keyword.canonical.strip().casefold()
        if not key or key in seen:
            raise ValueError("规范关键词为空或重复")
        seen.add(key)
        validate_evidence(
            [Evidence(article_id=source["id"], quote=keyword.quote)],
            {source["id"]: source},
        )
        if any(
            not alias.strip()
            or not any(
                alias in (source.get(k) or "") for k in ("title", "summary", "content")
            )
            for alias in keyword.aliases
        ):
            raise ValueError("关键词别名必须实际出现在本篇原文中")


def clean_aliases(result, source):
    """Retain every TAG; audit invalid aliases only when a valid alias remains."""
    cleaned = result.model_copy(deep=True)
    changes = []
    for i, keyword in enumerate(cleaned.keywords):
        valid = [alias for alias in keyword.aliases if alias.strip() and any(
            alias in (source.get(field) or '') for field in ('title','summary','content'))]
        if not valid:
            raise ValueError('关键词没有任何原文有效别名')
        if valid != keyword.aliases:
            changes.append({'path':f'keywords.{i}.aliases','original':keyword.aliases,
                            'replacement':valid,'reason':'aliases_absent_from_source'})
            keyword.aliases = valid
    return cleaned, changes


def validate_assignment(result, source, candidate_ids, keywords):
    for target in result.targets:
        if target.event_id is not None and target.event_id not in candidate_ids:
            raise ValueError("归属引用未知事件")
        validate_evidence(
            [Evidence(article_id=source["id"], quote=target.quote)],
            {source["id"]: source},
        )
        selected_tags(keywords, target.keyword_canonicals)


def selected_tags(keywords, canonicals):
    """Reuse selected keyword records, including aliases outside the scope quote."""
    available = {keyword["canonical"] for keyword in keywords}
    selected = set(canonicals)
    if not selected or len(selected) != len(canonicals) or not selected <= available:
        raise ValueError("归属TAG必须从输入canonical逐字选择，非空且不重复")
    return {
        term.strip().casefold()
        for keyword in keywords
        if keyword["canonical"] in selected
        for term in [keyword["canonical"], *keyword["aliases"]]
    }


async def refresh_event_tags(session, event):
    # Only committed assignment snapshots own TAGs; mutable indexes may be newer.
    latest = (
        select(func.max(Attempt.id))
        .where(Attempt.package_id == Membership.package_id,
               Attempt.stage == "assign", Attempt.state == "done")
        .correlate(Membership).scalar_subquery()
    )
    rows = await session.execute(
        select(Membership, Attempt).outerjoin(Attempt, Attempt.id == latest)
        .where(Membership.event_id == event.id)
    )
    tags = set()
    for member, attempt in rows:
        if attempt is None:
            raise ValueError(f"包{member.package_id}缺少成功归属快照，无法重建事件TAG")
        contributions = attempt.rules.get("membership_tags")
        if contributions is not None:
            contribution = contributions.get(str(event.id))
            if contribution is None:
                raise ValueError(f"包{member.package_id}成功归属快照缺少事件{event.id}")
            tags.update(contribution)
        else:
            # Legacy assignments did not select TAGs; retain their original full index.
            keywords = attempt.input.get("keywords")
            if not keywords:
                raise ValueError(f"包{member.package_id}历史归属缺少关键词快照，无法重建事件TAG")
            tags.update(selected_tags(keywords, [k["canonical"] for k in keywords]))
    event.tags = sorted(tags)


def research_scope(member, attempt):
    """Read event boundaries from the owning assignment, never the mutable index."""
    if attempt is None or not attempt.input.get("keywords"):
        raise ValueError(f"包{member.package_id}缺少成功归属关键词快照，无法研究事件")
    targets = attempt.rules.get("membership_targets")
    if targets is not None:
        target = targets.get(str(member.event_id))
        if target is None:
            raise ValueError(f"包{member.package_id}成功归属快照缺少事件{member.event_id}")
        others = [t for key, t in targets.items() if key != str(member.event_id)]
    else:
        # Older snapshots do not resolve new_key to event IDs. Use an unambiguous
        # persisted target, or explicitly leave the historical selection unknown.
        candidates = (attempt.output or {}).get("targets", [])
        matched = [t for t in candidates if t.get("event_id") == member.event_id]
        if not matched:
            matched = [t for t in candidates if t.get("event_id") is None
                       and t.get("quote") == member.evidence
                       and t.get("reason") == member.reason]
        target = matched[0] if len(matched) == 1 else None
        others = [t for t in candidates if t is not target] if target else []
    keywords = attempt.input["keywords"]
    canonicals = target.get("keyword_canonicals") if target else None
    if canonicals is not None:
        selected_tags(keywords, canonicals)
        keywords = [k for k in keywords if k["canonical"] in canonicals]
    return {
        "keywords": keywords,
        "keyword_canonicals": canonicals,
        "scope_selection": "assignment" if canonicals is not None else "legacy_unscoped",
        "other_scopes": [
            {"title": t["title"], "scope_quote": t["quote"], "reason": t["reason"]}
            for t in others
        ],
    }


def dirty(event, urgent=False):
    now = datetime.now(UTC)
    ready = (
        event.dirty_since is not None
        and event.state in ("pending", "processing")
        and event.due_at <= now
    )
    event.revision += 1
    event.dirty_since = event.dirty_since or now
    event.due_at = (
        now
        if urgent or ready
        else min(now + timedelta(minutes=5), event.dirty_since + timedelta(minutes=15))
    )
    event.updated_at = now
    event.state = "pending"
    event.error = None


async def model_config(session, config, stage):
    selected = config["index" if stage == "assign" else stage]
    provider = await provider_for(
        session,
        SimpleNamespace(provider_id=selected["provider_id"], model=selected["model"]),
    )
    if selected.get("reasoning_effort") is not None:
        provider.reasoning_effort = selected["reasoning_effort"]
    # Skill nodes are optional only for explicitly supplied isolated acceptance config.
    if selected.get("skill_id"):
        from alphapulse.models import SkillNode, SkillVersion
        from alphapulse.news_research.pipeline import BASE

        node = await session.get(SkillNode, selected['skill_id'])
        if node is None or node.scope != 'news' or node.kind != 'skill' or not node.published_version_id:
            raise ValueError('请选择已发布的新闻专用Skill')
        current = node
        while current:
            if not current.enabled:
                raise ValueError('新闻Skill或上级文件夹已停用')
            current = await session.get(SkillNode, current.parent_id) if current.parent_id else None
        version = await session.get(SkillVersion, node.published_version_id)
        rules = {'skill_id': node.id, 'version': version.version_no, 'content': version.content, 'base': BASE,
                 'model_config': {'provider_id': provider.id, 'model': provider.model,
                                  'max_tokens': provider.max_tokens, 'context_window': provider.context_window,
                                  'reasoning_effort': provider.reasoning_effort,
                                  'selected_reasoning_effort': selected.get('reasoning_effort')}}
        skill = BASE + '\n' + task_skill(version.content, stage)
    else:
        if not config.get("isolated_acceptance"):
            raise ValueError("请选择已发布的新闻专用Skill")
        skill = (
            INDEX_ASSIGN_SKILL if stage in ("index", "assign") else RESEARCH_SKILL
        )
        rules = {"content": skill}
        skill = task_skill(skill, stage)
    return (
        provider,
        skill,
        {
            **rules,
            "system_prompt": skill,
            "model": provider.model,
            "reasoning_effort": provider.reasoning_effort,
        },
    )


async def enroll(session, ids):
    news = list(await session.scalars(select(NewsItem).where(NewsItem.id == any_(list(ids)))))
    for row in news:
        await session.execute(
            insert(Package)
            .values(news_id=row.id, content_hash=row.content_hash)
            .on_conflict_do_nothing(index_elements=["news_id"])
        )
        package = await session.scalar(
            select(Package)
            .where(Package.news_id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # Read after acquiring the package lock, bypassing cached source objects.
        current_hash = await session.scalar(
            select(NewsItem.content_hash).where(NewsItem.id == row.id)
        )
        if package.content_hash != current_hash:
            package.content_hash = current_hash
            package.keywords = []
            package.state = "index_pending"
            for event in await session.scalars(
                select(Event)
                .join(Membership)
                .where(Membership.package_id == package.id)
                .with_for_update()
            ):
                dirty(event)
    await session.commit()


async def candidates(session, keywords, vocabulary=None):
    vocabulary = vocabulary if vocabulary is not None else await vocab.snapshot(session)
    groups = [vocab.expand([k["canonical"], *k["aliases"]], vocabulary) for k in vocab.normalize(keywords, vocabulary)]
    conditions = [
        or_(
            *(
                Event.tags.contains([term])
                | Event.title.icontains(term, autoescape=True)
                for term in group
            )
        )
        for group in groups
        if group
    ]
    if not conditions:
        raise ValueError("关键词索引为空，不能进行事件分类")
    # Compound aliases can omit the standalone entity TAG present in older reports.
    # Tokens widen candidate recall only; the model still decides event identity.
    tokens = sorted({token.casefold() for k in keywords for alias in k["aliases"]
                     for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{2,}(?![A-Za-z0-9])", alias)})
    fallback = [Event.tags.contains([token]) | Event.title.op("~*")(
        r"(^|[^A-Za-z0-9])" + re.escape(token) + r"([^A-Za-z0-9]|$)") for token in tokens]
    exact = or_(*conditions)
    score = sum(case((condition, 1), else_=0) for condition in conditions)
    fallback_score = sum((case((condition, 1), else_=0) for condition in fallback), literal(0))
    return list(await session.scalars(
        select(Event).where(Event.state != "retired", or_(exact, *fallback))
        .order_by(case((exact, 1), else_=0).desc(), score.desc(),
                  fallback_score.desc(), Event.updated_at.desc(), Event.id)
        .limit(60)
    ))



def package_matching(terms, vocabulary=None):
    term_groups = vocab.groups(terms, vocabulary) if vocabulary is not None else [[t] for t in dict.fromkeys(t.strip().lower() for t in terms if t.strip())]
    # Compare JSON values without changing literal aliases stored as evidence.
    words = (
        func.jsonb_array_elements(Package.keywords)
        .table_valued(column("value"))
        .alias("keyword")
    )
    aliases = (
        func.jsonb_array_elements_text(words.c.value.op("->")("aliases"))
        .table_valued(column("value"))
        .alias("alias")
    )
    conditions = []
    for group in term_groups:
        matches = []
        for term in group:
            canonical = func.lower(words.c.value.op("->>")("canonical")) == term
            alias = exists(select(1).select_from(aliases).where(func.lower(aliases.c.value) == term)).correlate(words)
            # Whole ASCII names/models recall compound TAGs without substring collisions.
            if re.fullmatch(r"[a-z][a-z0-9]{2,}", term):
                pattern = r"(^|[^A-Za-z0-9])" + re.escape(term) + r"([^A-Za-z0-9]|$)"
                canonical = canonical | words.c.value.op("->>")("canonical").op("~*")(pattern)
                alias = alias | exists(select(1).select_from(aliases).where(
                    aliases.c.value.op("~*")(pattern))).correlate(words)
            matches.append(NewsItem.title.icontains(term, autoescape=True)
                | ((Package.content_hash == NewsItem.content_hash)
                   & exists(select(1).select_from(words).where(canonical | alias)).correlate(Package)))
        conditions.append(or_(*matches))
    return or_(*conditions) if conditions else true(), sum(
        case((c, 1), else_=0) for c in conditions
    )


async def candidate_context(session, catalog):
    if not catalog:
        return []
    # Keep the first report and two recent reports without growing model input per event.
    reports = (
        select(
            Membership.event_id,
            NewsItem.id.label("news_id"),
            NewsItem.title,
            NewsItem.published_at,
            Membership.evidence.label("quote"),
            func.row_number()
            .over(
                partition_by=Membership.event_id,
                order_by=(NewsItem.published_at, NewsItem.id),
            )
            .label("position"),
            func.count().over(partition_by=Membership.event_id).label("total"),
            func.min(NewsItem.published_at)
            .over(partition_by=Membership.event_id)
            .label("first_reported_at"),
            func.max(NewsItem.published_at)
            .over(partition_by=Membership.event_id)
            .label("last_reported_at"),
        )
        .join(Package, Package.id == Membership.package_id)
        .join(NewsItem, NewsItem.id == Package.news_id)
        .where(
            Membership.event_id.in_([e.id for e in catalog]),
            Package.content_hash == NewsItem.content_hash,
            Package.state == "done",
        )
        .subquery()
    )
    contexts = {
        e.id: {
            "id": e.id,
            "revision": e.revision,
            "title": e.title,
            "reporting_range": None,
            "evidence": [],
        }
        for e in catalog
    }
    for row in await session.execute(
        select(reports)
        .where(or_(reports.c.position == 1, reports.c.position > reports.c.total - 2))
        .order_by(reports.c.event_id, reports.c.position)
    ):
        context = contexts[row.event_id]
        context["reporting_range"] = {
            "first": row.first_reported_at.isoformat(),
            "latest": row.last_reported_at.isoformat(),
        }
        context["evidence"].append(
            {
                "news_id": row.news_id,
                "title": row.title,
                "published_at": row.published_at.isoformat(),
                "quote": row.quote,
            }
        )
    return list(contexts.values())


async def process_package(identifier, config):
    async with SessionLocal() as session:
        package = await session.get(Package, identifier)
        assigning = package is not None and package.state == "assign_pending"
    if assigning:
        async with assignment_lock:
            await _process_package(identifier, config, "assign")
    else:
        await _process_package(identifier, config, "index")


async def _process_package(identifier, config, expected_stage):
    async with SessionLocal() as session:
        package = await session.get(Package, identifier, with_for_update=True)
        if package is None:
            return
        if package.state != expected_stage + "_pending":
            return
        news = await session.get(NewsItem, package.news_id)
        if package.content_hash != news.content_hash:
            await enroll(session, [news.id])
            return
        source = article(news)
        source_hash = package.content_hash
        stage = "index" if package.state == "index_pending" else "assign"
        if package.state not in ("index_pending", "assign_pending"):
            return
        catalog = []
        payload = {"task": stage, "article": source}
        attempt = Attempt(
            package_id=package.id,
            content_hash=source_hash,
            stage=stage,
            state="processing",
            input=payload,
            rules={},
        )
        session.add(attempt)
        package.state = stage + "_processing"
        await session.commit()
        attempt_id = attempt.id
        output = None
        used = None
        try:
            vocabulary = await vocab.snapshot(session)
            if stage == "assign":
                catalog = await candidates(session, package.keywords, vocabulary)
                payload = {
                    **payload,
                    "keywords": package.keywords,
                    "events": await candidate_context(session, catalog),
                }
                attempt.input = payload
            provider, skill, rules = await model_config(session, config, stage)
            attempt.rules = {**rules, "vocabulary": vocabulary}
            await session.commit()
            output, used = await complete(
                provider,
                skill,
                payload,
                IndexResult if stage == "index" else Assignment,
            )
            if stage == "index":
                cleaned, changes = clean_aliases(output, source)
                validate_keywords(cleaned, source)
                output = cleaned
                attempt.rules = {**attempt.rules, "output_adjustments": changes}
            else:
                validate_assignment(output, source, {e.id for e in catalog}, payload["keywords"])
            async with publication_lock:
                package = await session.get(
                    Package, identifier, populate_existing=True, with_for_update=True
                )
                current = await session.get(NewsItem, news.id, populate_existing=True)
                if (
                    package.content_hash != source_hash
                    or package.state != stage + "_processing"
                ):
                    attempt.state = "superseded"
                    attempt.output = output.model_dump(mode="json")
                    attempt.usage = used
                    await session.commit()
                    return
                if current.content_hash != source_hash:
                    attempt.state = "superseded"
                    await enroll(session, [current.id])
                elif stage == "index":
                    package.keywords = vocab.normalize([k.model_dump() for k in output.keywords], vocabulary)
                    package.state = "assign_pending"
                    attempt.state = "done"
                else:
                    previous = list(
                        await session.scalars(
                            select(Membership).where(
                                Membership.package_id == package.id
                            )
                        )
                    )
                    previous_ids = {m.event_id for m in previous}
                    assigned = set()
                    affected = {}
                    contributions = {}
                    resolved_targets = {}
                    for target in output.targets:
                        event = (
                            await session.get(
                                Event,
                                target.event_id,
                                populate_existing=True,
                                with_for_update=True,
                            )
                            if target.event_id
                            else None
                        )
                        if target.event_id and (
                            event is None or event.state == "retired"
                        ):
                            raise ValueError("候选事件已变化，请重试归属")
                        if event is None:
                            event = Event(title=target.title, tags=[], revision=0)
                            session.add(event)
                            await session.flush()
                        assigned.add(event.id)
                        affected[event.id] = event
                        contributions[str(event.id)] = sorted(selected_tags(
                            payload["keywords"], target.keyword_canonicals))
                        resolved_targets[str(event.id)] = target.model_dump(mode="json")
                        existing = next(
                            (m for m in previous if m.event_id == event.id), None
                        )
                        if existing is None:
                            session.add(
                                Membership(
                                    event_id=event.id,
                                    package_id=package.id,
                                    evidence=target.quote,
                                    reason=target.reason,
                                    change_kind=target.change_kind,
                                )
                            )
                        else:
                            existing.evidence = target.quote
                            existing.reason = target.reason
                            existing.change_kind = target.change_kind
                        if target.change_kind != "repost" or event.result is None:
                            dirty(event, target.urgent)
                    for old in previous:
                        if old.event_id not in assigned:
                            event = await session.get(
                                Event, old.event_id, with_for_update=True
                            )
                            dirty(event)
                            await session.delete(old)
                            affected[event.id] = event
                    package.state = "done"
                    attempt.state = "done"
                    attempt.rules = {**attempt.rules, "membership_tags": contributions,
                                     "membership_targets": resolved_targets}
                    await session.flush()
                    for event in affected.values():
                        await refresh_event_tags(session, event)
                    if not assigned and previous_ids:
                        raise ValueError("每个包至少归属一个事件")
                package.error = None
                attempt.output = output.model_dump(mode="json")
                attempt.usage = used
                await session.commit()
        except Exception as error:
            await session.rollback()
            attempt = await session.get(Attempt, attempt_id)
            attempt.state = "failed"
            attempt.error = str(error)[:2000]
            attempt.output = output.model_dump(mode="json") if output else None
            attempt.usage = used or getattr(error, "usage", None)
            # A failed old request must not replace a newly enrolled source revision.
            await session.execute(
                update(Package)
                .where(
                    Package.id == identifier,
                    Package.content_hash == source_hash,
                    Package.state == stage + "_processing",
                )
                .values(state=stage + "_failed", error=attempt.error)
            )
            await session.commit()


class Research(ResearchTools):
    def register_members(self, payload):
        self.member_sources = {a["news_id"]: a for a in payload["articles"]}
        self.member_packages = {p["id"]: p for p in payload["packages"]}
        self.sources.update({a["id"]: a for a in payload["articles"]})

    def restore(self, records):
        # Retry local searches against current data and reviewed vocabulary.
        from alphapulse.news_research.tools import TOOLS
        records = [r for r in records if r['tool'] in TOOLS
                   and r['tool'] not in ('search_packages', 'search_events')
                   and not (r['tool'] == 'read_news'
                            and r['arguments'].get('id') in getattr(self, 'member_sources', {}))]
        super().restore(records)
        self.records.extend(
            {**record, "reused": True}
            for record in records
            if not (
                isinstance(record.get("result"), dict) and "error" in record["result"]
            )
            and record["tool"] not in ("read_event", "read_package")
        )

    async def tool(self, name, arguments):
        member_sources = getattr(self, "member_sources", {})
        member_packages = getattr(self, "member_packages", {})
        if name in ("read_news", "read_package"):
            from alphapulse.news_research.tools import TOOLS

            args = TOOLS[name][0].model_validate(arguments)
            member = member_packages.get(args.id) if name == "read_package" else None
            news_id = (
                member["news_id"]
                if member
                else args.id
                if name == "read_news"
                else None
            )
            if news_id in member_sources:
                source = member_sources[news_id]
                if news_id in self.provided_news:
                    result = {
                        "article_id": source["id"],
                        "news_id": news_id,
                        "status": "already_provided",
                        "instruction": "该成员原文已完整提供，请复用现有正文及article_id；无需再次读取其他已提供成员，独立补证查询合并同轮。无可改变判断的新线索时提交并说明缺口。",
                    }
                    if member:
                        result.update(id=args.id, package_id=args.id)
                    self.records.append({"tool": name, "arguments": arguments, "result": result})
                    return result
                if self.counts["read"] >= 8:
                    raise ValueError("工具预算已用尽")
                self.counts["read"] += 1
                result = {**source, "article_id": source["id"]}
                if member:
                    result.update(id=args.id, package_id=args.id)
                self.provided_news[news_id] = source["id"]
                self.records.append(
                    {"tool": name, "arguments": arguments, "result": result}
                )
                return result
        if name in ("search_packages", "read_package", "search_events", "read_event"):
            from alphapulse.news_research.tools import TOOLS

            arguments = TOOLS[name][0].model_validate(arguments).model_dump()
            key = name + json.dumps(arguments, sort_keys=True)
            if key in self.cache:
                return self.cache[key]
            if name == "read_package" and arguments["id"] in self.provided_packages:
                return {
                    **self.provided_packages[arguments["id"]],
                    "status": "already_provided",
                }
            budget = "search" if name.startswith("search_") else "read"
            if self.counts[budget] >= ({"search": 4, "read": 8}[budget]):
                raise ValueError("工具预算已用尽")
            self.counts[budget] += 1
            async with SessionLocal() as session:
                if name.startswith("search_"):
                    query = arguments.get("query", "").strip()
                    if not query:
                        raise ValueError("检索词不能为空")
                    vocabulary = getattr(self, "vocabulary", None)
                    if vocabulary is None:
                        vocabulary = await vocab.snapshot(session)
                    terms = arguments.get("keywords") or query.split()
                    if name == "search_events":
                        conditions = [or_(*(Event.title.icontains(t, autoescape=True) | Event.tags.contains([t]) for t in group))
                                      for group in vocab.groups([query, *terms[:6]], vocabulary)]
                        phrase = or_(*(Event.title.icontains(t, autoescape=True) | Event.tags.contains([t]) for t in vocab.expand([query], vocabulary)))
                        score = sum(case((c, 1), else_=0) for c in conditions)
                        rows = await session.scalars(
                            select(Event)
                            .where(
                                Event.state != "retired",
                                Event.id != self.event["id"],
                                or_(*conditions),
                            )
                            .order_by(
                                case((phrase, 1), else_=0).desc(),
                                score.desc(),
                                Event.updated_at.desc(),
                                Event.id,
                            )
                            .limit(20)
                        )
                        result = [
                            {"id": r.id, "title": r.title, "tags": [t for t in r.tags
                             if any(term.lower() in t.lower() for term in terms[:6])]} for r in rows
                        ]
                    else:
                        # Retain the phrase in addition to the bounded search terms.
                        condition, score = package_matching([query, *terms[:6]], vocabulary)
                        phrase, _ = package_matching([query], vocabulary)
                        rows = await session.execute(
                            select(Package, NewsItem.content_hash, NewsItem.title)
                            .join(NewsItem)
                            .where(condition)
                            .order_by(
                                case((phrase, 1), else_=0).desc(),
                                score.desc(),
                                NewsItem.published_at.desc(),
                                Package.id,
                            )
                            .limit(20)
                        )
                        result = [
                            {"id": r.id, "news_id": r.news_id, "title": title,
                             "keywords": [{"canonical": k["canonical"], "aliases": [a for a in k["aliases"]
                                 if any(t.lower() in a.lower() for t in terms[:6])]}
                                 for k in r.keywords if any(t.lower() in word.lower()
                                     for t in terms[:6] for word in [k["canonical"], *k["aliases"]])]
                                 if r.content_hash == source_hash else []}
                            for r, source_hash, title in rows
                        ]
                elif name == "read_package":
                    row = await session.get(Package, arguments["id"])
                    if row is None:
                        raise ValueError("新闻包不存在")
                    news = await session.get(NewsItem, row.news_id)
                    result = article(news)
                    self.sources[news.id] = result
                    self.provided_news[news.id] = news.id
                    result = {
                        **result,
                        "id": row.id,
                        "article_id": news.id,
                        "package_id": row.id,
                    }
                else:
                    row = await session.get(Event, arguments["id"])
                    if row is None or row.id == self.event["id"]:
                        raise ValueError("只能读取其他事件")
                    version = await session.scalar(
                        select(Version)
                        .where(Version.event_id == row.id, Version.state == "done")
                        .order_by(Version.id.desc())
                        .limit(1)
                    )
                    if version is None:
                        raise ValueError("引用事件尚无成功研判")
                    self.read_event_ids.add(row.id)
                    self.read_event_versions[row.id] = version.id
                    result = {
                        "id": row.id,
                        "version_id": version.id,
                        "result": version.output,
                        "scope": "ai_summary_not_evidence",
                        "citation_instruction": "此版本中的历史evidence_refs不是本轮可用引用。请读取原文，并使用工具返回article_id（可能为负数），不得使用news_id或历史引用ID。",
                        "changed": version.revision != row.revision
                        or row.state != "done",
                        "package_ids": version.input["package_ids"],
                    }
            self.cache[key] = result
            self.records.append(
                {"tool": name, "arguments": arguments, "result": result}
            )
            return result
        return await super().tool(name, arguments)


def validate_timeline(result, package_news):
    for point in result.timeline_points:
        if not point.evidence_refs:
            raise ValueError("时间线节点缺少原文证据")
        if not set(point.package_ids) <= set(package_news):
            raise ValueError("时间线引用未知新闻包")
        quoted = {ref["article_id"] for ref in point.evidence_refs}
        if any(package_news[p] not in quoted for p in point.package_ids):
            raise ValueError("时间线必须有对应新闻包原文证据")
        if not point.package_ids and quoted & set(package_news.values()):
            raise ValueError("成员包时间线节点必须标注对应包ID")


async def process_event(identifier, config, force=False):
    async with SessionLocal() as session:
        event = await session.get(Event, identifier, with_for_update=True)
        if (
            event is None
            or event.state != "pending"
            or (not force and event.due_at > datetime.now(UTC))
        ):
            return
        members = list(
            await session.execute(
                select(Package, Membership, NewsItem)
                .join(Membership, Membership.package_id == Package.id)
                .join(NewsItem, NewsItem.id == Package.news_id)
                .where(Membership.event_id == identifier)
                .order_by(Package.id)
            )
        )
        if not members:
            event.state = "retired"
            await session.commit()
            return
        if any(
            p.state != "done" or p.content_hash != n.content_hash for p, m, n in members
        ):
            return
        payload = {
            "id": identifier,
            "event": {
                "id": identifier,
                "revision": event.revision,
                "title": event.title,
            },
            "previous": event.result,
            "package_ids": [p.id for p, m, n in members],
            "packages": [
                {
                    "id": p.id,
                    "news_id": n.id,
                    "content_hash": p.content_hash,
                    "scope_quote": m.evidence,
                    "reason": m.reason,
                }
                for p, m, n in members
            ],
            "articles": [article(n) for p, m, n in members],
        }
        version = Version(
            event_id=identifier,
            revision=event.revision,
            state="processing",
            input=payload,
            rules={},
        )
        session.add(version)
        event.state = "processing"
        await session.commit()
        version_id, revision = version.id, event.revision
        researcher = Research(event={"id": identifier, "revision": revision})
        output = None
        used = None
        preparations = []
        try:
            attempts = {a.package_id: a for a in await session.scalars(
                select(Attempt).where(Attempt.id.in_(
                    select(func.max(Attempt.id)).where(
                        Attempt.package_id.in_(payload["package_ids"]),
                        Attempt.stage == "assign", Attempt.state == "done",
                    ).group_by(Attempt.package_id)
                ))
            )}
            for p, _m, _n in members:
                attempt = attempts.get(p.id)
                if attempt is not None and attempt.content_hash != p.content_hash:
                    raise ValueError(f"包{p.id}成功归属快照与当前原文版本不一致")
            payload = {**payload, "packages": [
                {**package, **research_scope(m, attempts.get(p.id))}
                for package, (p, m, n) in zip(payload["packages"], members, strict=True)
            ]}
            version.input = payload
            researcher.register_members(payload)
            provider, skill, rules = await model_config(session, config, "research")
            researcher.vocabulary = await vocab.snapshot(session)
            version.rules = {**rules, "vocabulary": researcher.vocabulary}
            await session.commit()
            previous = await session.scalar(
                select(Version)
                .where(
                    Version.event_id == identifier,
                    Version.id != version_id,
                    Version.revision == revision,
                    Version.state == "failed",
                )
                .order_by(Version.id.desc())
                .limit(1)
            )
            if previous and previous.input == payload:
                researcher.restore(previous.evidence)
            request = payload
            if not researcher.fits(provider, skill, request, ResearchResult):
                extraction_rules = (
                    skill
                    + "\n当前事件："
                    + json.dumps(payload["event"], ensure_ascii=False)
                    + "\nextract只提取当前片段对本事件有用的事实与连续原文引用，不作评级。consolidate保留各来源、冲突、时间与引用，不新增事实。"
                )
                extracts, preparations = await extract_articles(
                    provider, extraction_rules, payload["articles"], scopes=payload["packages"]
                )
                if {
                    ref["article_id"]
                    for part in extracts
                    for ref in part["evidence_refs"]
                } != {a["id"] for a in payload["articles"]}:
                    raise ValueError("长输入提取遗漏成员原文，不能发布事件研判")
                request = {**payload, "articles": [], "extractions": extracts}
                request, extra = await compact_extractions(
                    provider, extraction_rules, request, ResearchResult,
                    # Final research carries member boundaries in the payload only.
                    lambda provider, _system, request, schema: researcher.fits(
                        provider, skill, request, schema),
                )
                preparations.extend(extra)
                researcher.sources.update({a["id"]: a for a in payload["articles"]})
            output, used = await researcher.run(
                provider, skill, request, ResearchResult
            )
            if preparations:
                used = {"preparation": preparations, "research": used}
            sources = {**{a["id"]: a for a in payload["articles"]}, **researcher.sources}
            raw_output = output.model_dump(mode="json")
            changes = align_quotes(raw_output, sources)
            if changes:
                output = ResearchResult.model_validate(raw_output)
            version.rules = {**version.rules, "output_adjustments": changes}
            validate_event(output, sources)
            if output.id != identifier:
                raise ValueError("返回事件ID不匹配")
            package_news = {p.id: n.id for p, m, n in members}
            validate_timeline(output, package_news)
            if len({r.version_id for r in output.references}) != len(output.references):
                raise ValueError("引用事件版本重复")
            if not {r.version_id for r in output.references} <= set(
                researcher.read_event_versions.values()
            ):
                raise ValueError("引用事件必须实际读取对应成功版本")
            async with publication_lock:
                current = await session.get(
                    Event, identifier, populate_existing=True, with_for_update=True
                )
                source_hashes = dict(
                    (
                        await session.execute(
                            select(NewsItem.id, NewsItem.content_hash).where(
                                NewsItem.id.in_(package_news.values())
                            )
                        )
                    ).all()
                )
                valid = current.revision == revision and all(
                    source_hashes[n.id] == p.content_hash for p, m, n in members
                )
                version.state = "done" if valid else "superseded"
                version.output = output.model_dump(mode="json")
                version.usage = used
                version.evidence = researcher.records
                if valid:
                    current.result = version.output
                    current.title = output.title
                    current.state = "done"
                    current.dirty_since = None
                    current.error = None
                    for ref in output.references:
                        session.add(
                            Reference(
                                version_id=version_id,
                                target_version_id=ref.version_id,
                                role=ref.role,
                                reason=ref.reason,
                            )
                        )
                else:
                    current.state = "pending"
                await session.commit()
        except Exception as error:
            await session.rollback()
            version = await session.get(Version, version_id)
            version.state = "failed"
            version.error = str(error)[:2000]
            version.evidence = researcher.records
            version.output = output.model_dump(mode="json") if output else None
            version.usage = used or {
                "preparation": preparations,
                "failed": getattr(error, "usage", None),
                "rounds": getattr(researcher, "usages", []),
            }
            event = await session.get(Event, identifier, with_for_update=True)
            event.state = "pending" if event.revision > revision else "failed"
            event.error = version.error
            await session.commit()
