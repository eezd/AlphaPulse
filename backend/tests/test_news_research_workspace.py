"""Behavioral contracts for one-news packages and reusable keyword indexes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as N
import json
import pytest


def test_storage_is_additive_and_membership_is_many_to_many(trading):
    from alphapulse.news_research.models import TABLES, Package, Membership

    assert all(t.name.startswith("news_research_") for t in TABLES)
    assert {c.name for c in Membership.__table__.primary_key} == {
        "event_id",
        "package_id",
    }
    assert any(
        {c.name for c in constraint.columns} == {"news_id"}
        for constraint in Package.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )


def test_keywords_require_original_aliases_and_literal_evidence(trading):
    from alphapulse.news_research.contracts import IndexResult
    from alphapulse.news_research.service import validate_keywords

    source = {"id": 7, "content": "Binance reported a security incident."}
    output = IndexResult(
        keywords=[
            {
                "canonical": "币安",
                "aliases": ["Binance"],
                "quote": "Binance reported a security incident.",
            }
        ]
    )
    validate_keywords(output, source)
    output.keywords[0].aliases = ["Invented"]
    with pytest.raises(ValueError, match="别名"):
        validate_keywords(output, source)
    output.keywords[0].aliases = ["Binance"]
    output.keywords[0].quote = "Binance was hacked"
    with pytest.raises(ValueError, match="原文"):
        validate_keywords(output, source)


@pytest.mark.parametrize("canonical", ["币安", "Binance", "MI455X"])
def test_chinese_assignment_preserves_indexed_names_and_english_evidence(trading, canonical):
    from alphapulse.news_research.contracts import Assignment
    from alphapulse.news_research.service import validate_assignment

    quote = "Binance announced scheduled maintenance for MI455X."
    source = {"id": 7, "content": quote}
    keywords = [{"canonical": canonical, "aliases": ["Binance", "MI455X"], "quote": quote}]
    result = Assignment(targets=[{
        "event_id": None, "new_key": "maintenance", "title": "币安计划维护公告",
        "reason": "原文宣布计划维护，独立建立事件。", "quote": quote,
        "keyword_canonicals": [canonical], "change_kind": "new_fact", "urgent": False,
    }])
    validate_assignment(result, source, set(), keywords)
    assert result.targets[0].keyword_canonicals == [canonical]
    result.targets[0].quote = "币安宣布计划维护。"
    with pytest.raises(ValueError, match="原文"):
        validate_assignment(result, source, set(), keywords)
    result.targets[0].quote = quote
    result.targets[0].keyword_canonicals = ["改写的关键词"]
    with pytest.raises(ValueError, match="逐字"):
        validate_assignment(result, source, set(), keywords)


def test_one_package_can_be_assigned_to_multiple_events_without_splitting(trading):
    from alphapulse.news_research.contracts import Assignment
    from alphapulse.news_research.service import validate_assignment

    base = {
        "new_key": None,
        "title": "Incident",
        "reason": "Follow-up",
        "quote": "Original",
        "keyword_canonicals": ["Original"],
        "change_kind": "new_fact",
        "urgent": False,
    }
    result = Assignment(targets=[{**base, "event_id": 1}, {**base, "event_id": 2}])
    keywords = [{"canonical": "Original", "aliases": ["Original"]}]
    validate_assignment(result, {"id": 8, "content": "Original"}, {1, 2}, keywords)
    with pytest.raises(ValueError, match="未知事件"):
        validate_assignment(result, {"id": 8, "content": "Original"}, {1}, keywords)
    with pytest.raises(ValueError, match="重复"):
        Assignment(targets=[{**base, "event_id": 1}, {**base, "event_id": 1}])


def test_event_refresh_keeps_hard_deadline_and_urgency(trading):
    from alphapulse.news_research.service import dirty

    now = datetime.now(UTC)
    event = N(
        revision=2,
        state="done",
        dirty_since=now - timedelta(minutes=14),
        due_at=now,
        error="Old",
    )
    dirty(event)
    assert event.due_at <= event.dirty_since + timedelta(minutes=15)
    assert event.revision == 3 and event.state == "pending" and event.error is None
    dirty(event, True)
    immediate = event.due_at
    dirty(event)
    assert event.due_at <= datetime.now(UTC) and event.due_at >= immediate


@pytest.mark.parametrize("urgent", [False, True])
def test_new_event_waits_for_aggregation_unless_urgent(trading, urgent):
    from alphapulse.news_research.service import dirty

    event = N(
        revision=0,
        state="pending",
        dirty_since=None,
        due_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    dirty(event, urgent)
    assert event.due_at - event.dirty_since == timedelta(minutes=0 if urgent else 5)


@pytest.mark.parametrize("state,deferred", [("failed", False), ("pending", False), ("pending", True), ("done", False)])
def test_event_retry_preserves_content_revision_unless_done_or_deferred(trading, state, deferred):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.api import retry_event

    event = N(
        id=1, revision=7,
        state=state,
        dirty_since=None,
        due_at=datetime.now(UTC),
        error="Previous failure",
    )
    session = N(get=AsyncMock(return_value=event), scalar=AsyncMock(return_value=12 if deferred else None), commit=AsyncMock())
    asyncio.run(retry_event(1, session))
    assert event.revision == (8 if state == "done" or deferred else 7)
    assert event.state == "pending" and event.error is None
    assert event.due_at <= datetime.now(UTC)


@pytest.mark.parametrize("revision", [None, 2])
def test_news_detail_does_not_attach_old_keywords_to_updated_original(
    trading, revision
):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.api import news_package

    news = N(revision_no=2, content_hash="new")
    package = N(content_hash="old", keywords=[{"canonical": "Old fact"}])
    session = N(
        get=AsyncMock(return_value=news), scalar=AsyncMock(return_value=package)
    )
    result = asyncio.run(news_package(1, session, revision_no=revision))
    assert result.data == []


def test_two_stage_contract_rejects_package_summaries(trading):
    from alphapulse.news_research.contracts import (
        IndexResult,
        Assignment,
        ResearchResult,
    )

    assert set(IndexResult.model_fields) == {"keywords"}
    assert set(Assignment.model_fields) == {"targets"}
    assert "references" in ResearchResult.model_fields
    assert "package_correction" not in ResearchResult.model_fields


def test_timeline_distinguishes_member_progress_from_external_background(trading):
    from alphapulse.news_research.service import validate_timeline

    point = N(
        package_ids=[17], evidence_refs=[{"article_id": -3, "quote": "External source"}]
    )
    result = N(timeline_points=[point])
    with pytest.raises(ValueError, match="对应新闻包"):
        validate_timeline(result, {17: 463205})
    point.package_ids = []
    validate_timeline(result, {17: 463205})
    point.evidence_refs = [{"article_id": 463205, "quote": "Member source"}]
    with pytest.raises(ValueError, match="标注"):
        validate_timeline(result, {17: 463205})
    point.package_ids = [17]
    validate_timeline(result, {17: 463205})


def test_retry_reuses_original_snapshots_and_keeps_the_evidence_auditable(trading):
    import asyncio
    from alphapulse.news_research.service import Research

    research = Research(event={"id": 8})
    snapshot = {"article_id": -1, "news_id": 7, "content": "Original evidence"}
    research.restore(
        [{"tool": "read_news", "arguments": {"id": 7}, "result": snapshot}]
    )
    result = asyncio.run(research.tool("read_news", {"id": 7}))
    assert result == snapshot
    assert research.counts["read"] == 0
    assert research.records[0]["reused"] is True
    assert research.records[0]["result"]["article_id"] == -1


@pytest.mark.parametrize("tool", ["search_packages", "search_events"])
@pytest.mark.parametrize("cached", [[], [{"id": 1}]])
def test_retry_refreshes_local_search_with_current_vocabulary(
    trading, monkeypatch, tool, cached
):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research import service

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.execute.return_value = [
        (N(id=2, news_id=12, content_hash="current", keywords=[]), "current", "Fresh news")
    ]
    session.scalars.return_value = [N(id=2, title="Fresh event", tags=["binance"])]
    monkeypatch.setattr(service, "SessionLocal", lambda: session)
    reader = service.Research(event={"id": 8})
    reader.vocabulary = {
        "revision": 2,
        "entries": [{"canonical": "币安", "aliases": ["Binance", "BNC"]}],
    }
    arguments = {"query": "BNC", "keywords": []}
    reader.restore([{"tool": tool, "arguments": arguments, "result": cached}])

    result = asyncio.run(reader.tool(tool, arguments))

    assert [row["id"] for row in result] == [2]
    assert reader.counts["search"] == 1
    assert len(reader.records) == 1 and not reader.records[0].get("reused")




def test_settings_contract_rejects_third_stage_and_unknown_efforts(trading):
    from alphapulse.news_research.api import SettingsInput

    model = {
        "provider_id": 1,
        "model": "selected",
        "skill_id": 1,
        "reasoning_effort": None,
    }
    assert SettingsInput(index=model, research=model).concurrency == 3
    with pytest.raises(ValueError, match="extra_forbidden"):
        SettingsInput(index=model, research=model, outlook=model)
    with pytest.raises(ValueError):
        SettingsInput(
            index={**model, "reasoning_effort": "unsupported"}, research=model
        )


@pytest.mark.parametrize("tool", ["read_news", "read_package"])
def test_compressed_members_read_frozen_positive_sources_and_reject_old_cache(
    trading, tool
):
    import asyncio
    from alphapulse.news_research.service import Research, validate_timeline

    research = Research(event={"id": 8})
    source = {"id": 101, "news_id": 101, "content": "Frozen original"}
    research.register_members(
        {"articles": [source], "packages": [{"id": 10, "news_id": 101}]}
    )
    research.provided_packages = {10: {"id": 10, "news_id": 101}}
    research.restore(
        [
            {
                "tool": "read_news",
                "arguments": {"id": 101},
                "result": {"article_id": -1, "news_id": 101, "content": "Old cache"},
            }
        ]
    )
    assert not research.provided_news and -1 not in research.sources
    result = asyncio.run(
        research.tool(tool, {"id": 101 if tool == "read_news" else 10})
    )
    assert result["article_id"] == 101 and result["content"] == "Frozen original"
    validate_timeline(
        N(
            timeline_points=[
                N(
                    package_ids=[10],
                    evidence_refs=[
                        {"article_id": result["article_id"], "quote": "Frozen original"}
                    ],
                )
            ]
        ),
        {10: 101},
    )
    repeated = asyncio.run(research.tool("read_news", {"id": 101}))
    assert repeated["status"] == "already_provided" and research.counts["read"] == 1


def test_assignment_gate_serializes_only_assignment_and_releases_after_failure(
    trading, monkeypatch
):
    import asyncio
    from alphapulse.news_research import service

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, identifier):
            return N(state="index_pending" if identifier == 3 else "assign_pending")

    async def scenario():
        monkeypatch.setattr(service, "assignment_lock", asyncio.Lock())
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def process(identifier, config, stage):
            calls.append((identifier, stage))
            if identifier == 1:
                entered.set()
                await release.wait()
                raise ValueError("Failed model")

        monkeypatch.setattr(service, "SessionLocal", Session)
        monkeypatch.setattr(service, "_process_package", process)
        first = asyncio.create_task(service.process_package(1, {}))
        await entered.wait()
        second = asyncio.create_task(service.process_package(2, {}))
        await service.process_package(3, {})
        assert calls == [(1, "assign"), (3, "index")]
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        assert calls[-1] == (2, "assign") and not service.assignment_lock.locked()

    asyncio.run(scenario())


def test_digest_tags_are_selected_per_membership(trading):
    from alphapulse.news_research.service import selected_tags

    keywords = [
        {"canonical": "中国人民银行", "aliases": ["央行"]},
        {"canonical": "美国民主党", "aliases": ["民主党"]},
        {"canonical": "股票回购增持再贷款", "aliases": ["股票回购增持再贷款"]},
        {"canonical": "日经225指数", "aliases": ["日经225指数"]},
    ]
    assert selected_tags(keywords, ["中国人民银行", "股票回购增持再贷款"]) == {
        "中国人民银行", "央行", "股票回购增持再贷款",
    }
    assert selected_tags(keywords, ["美国民主党"]) == {"美国民主党", "民主党"}
    assert len(keywords) == 4


@pytest.mark.parametrize("selection", [[], ["Invented"], ["AMD", "AMD"], ["amd"]])
def test_assignment_rejects_empty_unknown_duplicate_or_rewritten_tags(trading, selection):
    from alphapulse.news_research.contracts import Assignment
    from alphapulse.news_research.service import validate_assignment

    with pytest.raises(ValueError):
        result = Assignment(targets=[{
            "event_id": 1, "new_key": None, "title": "AMD", "reason": "Same event",
            "quote": "AMD shipped", "keyword_canonicals": selection,
            "change_kind": "new_fact", "urgent": False,
        }])
        validate_assignment(result, {"id": 1, "content": "AMD shipped"}, {1},
                            [{"canonical": "AMD", "aliases": ["AMD"]}])


def test_assignment_keeps_entity_aliases_outside_scope_quote(trading):
    from alphapulse.news_research.contracts import Assignment
    from alphapulse.news_research.service import selected_tags, validate_assignment

    source = {"id": 1, "content": "高盛警示美股风险。研报作者为鲍比·莫拉维（Bobby Molavi）。"}
    keywords = [
        {"canonical": "高盛", "aliases": ["高盛"]},
        {"canonical": "鲍比·莫拉维", "aliases": ["鲍比·莫拉维", "Bobby Molavi"]},
        {"canonical": "美国民主党", "aliases": ["民主党"]},
    ]
    selected = ["高盛", "鲍比·莫拉维"]
    result = Assignment(targets=[{
        "event_id": 1, "new_key": None, "title": "高盛警示美股风险", "reason": "Same report",
        "quote": "高盛警示美股风险。", "keyword_canonicals": selected,
        "change_kind": "new_fact", "urgent": False,
    }])
    validate_assignment(result, source, {1}, keywords)
    assert selected_tags(keywords, selected) == {"高盛", "鲍比·莫拉维", "bobby molavi"}


def test_event_tags_are_rebuilt_instead_of_retaining_removed_noise(trading):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.service import refresh_event_tags

    event = N(id=1, tags=["民主党", "旧事件"])
    session = N(execute=AsyncMock(return_value=[(
        N(package_id=1, evidence="央行下调利率"),
        N(rules={"membership_tags": {"1": ["中国人民银行", "央行"]}}),
    )]))
    asyncio.run(refresh_event_tags(session, event))
    assert event.tags == ["中国人民银行", "央行"]
    session.execute.return_value = []
    asyncio.run(refresh_event_tags(session, event))
    assert event.tags == []


def test_event_rebuild_uses_successful_snapshot_not_mutable_package_index(trading):
    import asyncio
    from unittest.mock import AsyncMock
    from sqlalchemy.dialects import postgresql
    from alphapulse.news_research.service import refresh_event_tags

    session = N(execute=AsyncMock(return_value=[(
        N(package_id=1, evidence="公司遭到黑客攻击。"),
        N(rules={"membership_tags": {"1": ["旧公司", "黑客攻击"]}}),
    )]))
    event = N(id=1, tags=["英伟达"])
    asyncio.run(refresh_event_tags(session, event))
    assert event.tags == ["旧公司", "黑客攻击"]
    statement = str(session.execute.call_args.args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "news_research_packages" not in statement
    assert "news_research_attempts.state = 'done'" in statement
    assert "max(news_research_attempts.id)" in statement


def test_legacy_assignment_retains_its_original_keyword_snapshot(trading):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.service import refresh_event_tags

    event = N(id=1, tags=[])
    session = N(execute=AsyncMock(return_value=[(
        N(package_id=1, evidence="公司遭到黑客攻击。"),
        N(rules={}, input={"keywords": [
            {"canonical": "旧公司", "aliases": ["OldCorp"]},
        ]}),
    )]))
    asyncio.run(refresh_event_tags(session, event))
    assert event.tags == ["oldcorp", "旧公司"]


@pytest.mark.parametrize("snapshot", [None, N(rules={}, input={}),
                                      N(rules={"membership_tags": {"2": ["wrong"]}})])
def test_event_rebuild_fails_without_owning_assignment_snapshot(trading, snapshot):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.service import refresh_event_tags

    event = N(id=1, tags=["preserved"])
    session = N(execute=AsyncMock(return_value=[(N(package_id=1), snapshot)]))
    with pytest.raises(ValueError, match="快照"):
        asyncio.run(refresh_event_tags(session, event))
    assert event.tags == ["preserved"]


@pytest.mark.parametrize("offset", ["+03:30", "+08:00", "-05:00"])
@pytest.mark.parametrize("precision,day", [("day", 4), ("month", 1)])
def test_coarse_timeline_dates_are_consistent_calendar_markers(trading, offset, precision, day):
    from alphapulse.news_research.contracts import Point

    p = Point(occurred_at=f"2026-11-04T13:25:00{offset}", time_precision=precision,
              planned_at=f"2026-12-04T13:25:00{offset}", planned_precision=precision,
              time_text="4日", reported_at=None, description="公告", verification="supported",
              evidence_refs=[{"article_id": 1, "quote": "4日公告"}], package_ids=[1])
    assert p.occurred_at == datetime(2026, 11, day, tzinfo=UTC)
    assert p.planned_at == datetime(2026, 12, day, tzinfo=UTC)
    assert p.time_text == "4日"


def test_minute_precision_retains_real_instant_and_unknown_stays_null(trading):
    from alphapulse.news_research.contracts import Point

    p = Point(occurred_at="2026-11-04T13:25:00+03:30", time_precision="minute",
              time_text="13:25", reported_at=None, description="公告", verification="supported",
              evidence_refs=[{"article_id": 1, "quote": "13:25公告"}], package_ids=[1])
    assert p.occurred_at.isoformat() == "2026-11-04T13:25:00+03:30"
    p = Point(**{**p.model_dump(), "occurred_at": None, "time_precision": "unknown"})
    assert p.occurred_at is None


def test_research_scope_uses_resolved_assignment_and_keeps_full_selected_records(trading):
    from copy import deepcopy
    from alphapulse.news_research.service import research_scope

    keywords = [
        {"canonical": "高盛", "aliases": ["高盛"], "quote": "高盛警示风险"},
        {"canonical": "鲍比·莫拉维", "aliases": ["Bobby Molavi"], "quote": "作者Bobby Molavi"},
        {"canonical": "美国民主党", "aliases": ["民主党"], "quote": "民主党选情更新"},
    ]
    main = {"title": "风险报告", "quote": "高盛警示风险", "reason": "同一报告",
            "keyword_canonicals": ["高盛", "鲍比·莫拉维"]}
    other = {"title": "选情更新", "quote": "民主党选情更新", "reason": "独立事项",
             "keyword_canonicals": ["美国民主党"]}
    attempt = N(input={"keywords": keywords},
                rules={"membership_targets": {"1": main, "2": other}})
    before = deepcopy(attempt.input)
    scope = research_scope(N(package_id=10, event_id=1), attempt)
    assert scope["keywords"] == keywords[:2]
    assert scope["keyword_canonicals"] == ["高盛", "鲍比·莫拉维"]
    assert scope["scope_selection"] == "assignment"
    assert scope["other_scopes"] == [{"title": "选情更新", "scope_quote": "民主党选情更新", "reason": "独立事项"}]
    assert research_scope(N(package_id=10, event_id=2), attempt)["keywords"] == keywords[2:]
    assert attempt.input == before


@pytest.mark.parametrize("existing", [False, True])
def test_research_recovers_unambiguous_selected_tags_from_older_assignment(trading, existing):
    from alphapulse.news_research.service import research_scope

    keyword = {"canonical": "触雷", "aliases": ["水雷"]}
    target = {"event_id": 1 if existing else None, "quote": "四艘油轮触雷", "reason": "具体事故",
              "keyword_canonicals": ["触雷"]}
    other = {"event_id": None, "title": "船舶拦截", "quote": "另有两艘船被拦截", "reason": "独立行动"}
    attempt = N(input={"keywords": [keyword, {"canonical": "拦截", "aliases": ["拦截"]}]},
                rules={}, output={"targets": [target, other]})
    scope = research_scope(N(package_id=10, event_id=1, evidence=target["quote"], reason=target["reason"]), attempt)
    assert scope["keywords"] == [keyword]
    assert scope["other_scopes"][0]["scope_quote"] == "另有两艘船被拦截"


@pytest.mark.parametrize("targets", [[], [
    {"event_id": None, "quote": "同一引文", "reason": "同一理由", "keyword_canonicals": ["甲"]},
    {"event_id": None, "quote": "同一引文", "reason": "同一理由", "keyword_canonicals": ["乙"]},
]])
def test_research_legacy_does_not_invent_a_selected_scope(trading, targets):
    from alphapulse.news_research.service import research_scope

    keywords = [{"canonical": "甲", "aliases": ["甲"]}, {"canonical": "乙", "aliases": ["乙"]}]
    scope = research_scope(N(package_id=10, event_id=1, evidence="同一引文", reason="同一理由"),
                           N(input={"keywords": keywords}, rules={}, output={"targets": targets}))
    assert scope["scope_selection"] == "legacy_unscoped"
    assert scope["keyword_canonicals"] is None and scope["keywords"] == keywords
    assert scope["other_scopes"] == []


def test_legacy_research_retains_known_sibling_boundaries_without_inventing_tags(trading):
    from alphapulse.news_research.service import research_scope

    keywords = [{"canonical": "甲", "aliases": ["甲"]}]
    own = {"event_id": 1, "title": "事项甲", "quote": "原文甲", "reason": "主线甲"}
    other = {"event_id": 2, "title": "事项乙", "quote": "原文乙", "reason": "主线乙"}
    scope = research_scope(N(package_id=10, event_id=1),
                           N(input={"keywords": keywords}, rules={}, output={"targets": [own, other]}))
    assert scope["scope_selection"] == "legacy_unscoped" and scope["keyword_canonicals"] is None
    assert scope["keywords"] == keywords
    assert scope["other_scopes"] == [{"title": "事项乙", "scope_quote": "原文乙", "reason": "主线乙"}]


@pytest.mark.parametrize("attempt", [None, N(input={}, rules={}), N(
    input={"keywords": [{"canonical": "甲", "aliases": ["甲"]}]},
    rules={"membership_targets": {"2": {}}},
)])
def test_research_fails_without_owning_assignment_snapshot(trading, attempt):
    from alphapulse.news_research.service import research_scope

    with pytest.raises(ValueError, match="快照"):
        research_scope(N(package_id=10, event_id=1), attempt)


@pytest.mark.parametrize("tool,identifier", [("read_news", 101), ("read_package", 10)])
def test_provided_member_reads_are_audited_without_reading_or_spending_budget(trading, tool, identifier):
    import asyncio
    from alphapulse.news_research.service import Research

    reader = Research(event={"id": 1})
    source = {"id": 101, "news_id": 101, "content": "完整原文"}
    payload = {"articles": [source], "packages": [{"id": 10, "news_id": 101}]}
    reader.register_members(payload)
    reader.provided_news = {101: 101}
    result = asyncio.run(reader.tool(tool, {"id": identifier}))
    assert result["status"] == "already_provided" and result["article_id"] == 101
    assert "复用" in result["instruction"] and "独立补证查询合并同轮" in result["instruction"]
    assert "content" not in result and reader.counts["read"] == 0
    assert reader.records == [{"tool": tool, "arguments": {"id": identifier}, "result": result}]
    retry = Research(event={"id": 1})
    retry.register_members(payload)
    retry.restore(reader.records)
    assert not retry.provided_news and not retry.records
    reread = asyncio.run(retry.tool(tool, {"id": identifier}))
    assert reread["content"] == "完整原文" and retry.counts["read"] == 1


@pytest.mark.parametrize("needs_compaction", [False, True])
def test_long_event_compaction_uses_final_research_context(trading, monkeypatch, needs_compaction):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research import service
    from alphapulse.news_research.contracts import RESEARCH_SKILL, ResearchResult
    from alphapulse.news_research.models import Version
    from alphapulse.news_research import pipeline

    provider = N(max_tokens=32000, context_window=250000)
    skill = pipeline.BASE + "\n" + RESEARCH_SKILL
    quote = "该公司披露事故调查及资金追踪的最新进展。"
    keywords = [{"canonical": f"因素{j}", "aliases": [f"Factors{j}"], "quote": quote * 4}
                for j in range(8)]
    target = {"title": "事故调查", "quote": quote, "reason": "同一事故的后续处置。",
              "keyword_canonicals": [k["canonical"] for k in keywords]}
    event = N(id=1, revision=1, title="事故调查", state="pending", result=None,
              due_at=datetime.now(UTC) - timedelta(seconds=1))
    members, attempts, versions = [], [], []
    for identifier in range(1, 41):
        news = N(id=identifier, title=quote, summary="", content_text=quote * 2000,
                 content_hash=str(identifier), url="https://example.invalid/event",
                 published_at=datetime.now(UTC))
        members.append((N(id=identifier, news_id=identifier, state="done", content_hash=news.content_hash),
                        N(event_id=1, package_id=identifier, evidence=quote, reason=target["reason"]), news))
        attempts.append(N(package_id=identifier, content_hash=news.content_hash,
                          input={"keywords": keywords}, rules={"membership_targets": {"1": target}}))
    extracts = [{"facts": "detail " * 400 if needs_compaction else "调查继续。",
                 "evidence_refs": [{"article_id": p.id, "quote": quote}]} for p, _, _ in members]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def add(self, version):
            version.id = 1
            versions.append(version)

    session = Session()
    session.get = AsyncMock(side_effect=lambda model, *args, **kwargs: versions[0] if model is Version else event)
    session.execute = AsyncMock(side_effect=[members, N(all=lambda: [(n.id, n.content_hash) for _, _, n in members])])
    session.scalars = AsyncMock(return_value=attempts)
    session.scalar = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    monkeypatch.setattr(service, "SessionLocal", lambda: session)
    monkeypatch.setattr(service, "model_config", AsyncMock(return_value=(provider, skill, {})))
    monkeypatch.setattr(service.vocab, "snapshot", AsyncMock(return_value={}))
    extract = AsyncMock(return_value=(extracts, []))
    monkeypatch.setattr(service, "extract_articles", extract)
    # Quote and timeline validation are independent of this context-budget regression.
    monkeypatch.setattr(service, "validate_event", lambda *args: None)
    monkeypatch.setattr(service, "validate_timeline", lambda *args: None)

    async def consolidate(provider, rules, payload, schema):
        assert schema is pipeline.EvidenceSelection
        assert "当前事件：" in rules and "当前事件及各成员归属边界" not in rules
        assert "extract和consolidate生成的facts等叙述也必须用简体中文" in rules
        assert "time_text及取自原文的period_text逐字保留原语言和字符" in rules
        assert pipeline.fits_context(provider, rules, payload, schema)
        return schema(facts="调查继续。", evidence_ids=[ref["id"] for ref in payload["evidence"]]), {"mock": True}

    complete = AsyncMock(side_effect=consolidate)
    monkeypatch.setattr(pipeline, "complete", complete)

    research_requests = []

    async def research(reader, provider, rules, request, schema):
        assert rules == skill and schema is ResearchResult
        assert reader.fits(provider, rules, request, schema)
        assert request["articles"] == [] and len(request["packages"]) == 40
        assert {ref["article_id"] for part in request["extractions"] for ref in part["evidence_refs"]} == set(range(1, 41))
        research_requests.append(request)
        return N(id=1, title=event.title, references=[], model_dump=lambda **kwargs: {"id": 1}), {}

    monkeypatch.setattr(service.Research, "run", research)
    asyncio.run(service.process_event(1, {}))
    assert versions[0].state == "done", versions[0].error
    assert event.state == "done" and len(research_requests) == 1
    assert bool(complete.await_count) == needs_compaction
    boundary_rules = extract.await_args.args[1]
    assert "extract和consolidate生成的facts等叙述也必须用简体中文" in boundary_rules
    compressed = {**versions[0].input, "articles": [], "extractions": extracts}
    reader = service.Research()
    assert not reader.fits(provider, skill, versions[0].input, ResearchResult)
    assert len(extract.await_args.kwargs["scopes"]) == 40
    assert all(p["keywords"] == keywords for p in extract.await_args.kwargs["scopes"])
    assert len(boundary_rules) < len(json.dumps(versions[0].input["packages"], ensure_ascii=False))
    assert reader.fits(provider, skill, compressed, ResearchResult) != needs_compaction


@pytest.mark.parametrize("failure_at", [None, "evidence", "timeline", "publication"])
def test_quote_adjustment_audit_survives_validation_and_publication_failures(trading, monkeypatch, failure_at):
    import asyncio
    import copy
    from unittest.mock import AsyncMock
    from alphapulse.news_research import service
    from alphapulse.news_research.contracts import ResearchResult
    from alphapulse.news_research.models import Version

    quote, original_quote = "官员“感到担忧”。", "官员“感到担忧” 。"
    event = N(id=1, revision=1, title="官员回应", state="pending", result=None,
              due_at=datetime.now(UTC) - timedelta(seconds=1))
    news = N(id=101, title="官员回应", summary="", content_text=quote, content_hash="hash",
             url="https://example.invalid/news", published_at=datetime.now(UTC))
    package = N(id=7, news_id=101, state="done", content_hash="hash")
    member = N(event_id=1, package_id=7, evidence=quote, reason="同一回应")
    assignment = N(package_id=7, content_hash="hash", input={"keywords": [
        {"canonical": "官员", "aliases": ["官员"], "quote": quote}]},
        rules={"membership_targets": {"1": {"keyword_canonicals": ["官员"]}}})
    output = ResearchResult(
        id=1, title="官员回应", summary="官员表达担忧。", category="政策", entities=[],
        risk_stars=1, importance_stars=2, direction="unknown", verification="reported", confidence="low",
        reasoning="依据报道。", uncertainty="缺少独立佐证。", facts="官员表达担忧。", timeline="官员回应。",
        asset_impacts="未知。", opinions="持续观察。", counter_evidence="未发现。", watchpoints="后续回应。",
        claims=[{"statement": "官员表达担忧。", "verdict": "supported", "evidence_refs": [
            {"article_id": 101, "quote": "不存在的引文" if failure_at == "evidence" else quote}]}],
        evidence_refs=[{"article_id": 101, "quote": original_quote}],
        timeline_points=[{"occurred_at": None, "time_precision": "unknown", "time_text": "",
            "reported_at": None, "description": "官员表达担忧。", "verification": "supported",
            "package_ids": [999 if failure_at == "timeline" else 7],
            "evidence_refs": [{"article_id": 101, "quote": quote}]}], references=[])
    original = output.model_dump(mode="json")
    versions, saved_rules = [], {}

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def add(self, row):
            row.id = 1
            versions.append(row)
        async def commit(self):
            nonlocal saved_rules
            if versions[0].state == "done" and failure_at == "publication":
                raise RuntimeError("发布写入失败")
            saved_rules = copy.deepcopy(versions[0].rules)
        async def rollback(self):
            # Rollback discards uncommitted JSON changes, as the real ORM does.
            versions[0].rules = copy.deepcopy(saved_rules)

    session = Session()
    session.get = AsyncMock(side_effect=lambda model, *args, **kwargs: versions[0] if model is Version else event)
    session.execute = AsyncMock(side_effect=[[(package, member, news)], N(all=lambda: [(101, "hash")])])
    session.scalars = AsyncMock(return_value=[assignment])
    session.scalar = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "SessionLocal", lambda: session)
    monkeypatch.setattr(service, "model_config", AsyncMock(return_value=(
        N(max_tokens=32000, context_window=250000), "研究规则", {"system_prompt": "研究规则"})))
    monkeypatch.setattr(service.vocab, "snapshot", AsyncMock(return_value={}))
    monkeypatch.setattr(service.Research, "run", AsyncMock(return_value=(output, {"input_tokens": 10})))

    asyncio.run(service.process_event(1, {}))
    version = versions[0]
    assert version.state == ("failed" if failure_at else "done"), version.error
    if failure_at:
        assert {"evidence": "原文引用", "timeline": "未知新闻包", "publication": "发布写入失败"}[failure_at] in version.error
    assert version.output["evidence_refs"][0]["quote"] == quote
    assert version.rules["raw_output"] == original
    assert version.rules["output_adjustments"] == [{
        "path": ".evidence_refs.0.quote", "original": original_quote, "replacement": quote,
        "reason": "unique_punctuation_whitespace_alignment"}]
    assert version.rules["system_prompt"] == "研究规则"
    assert news.content_text == quote and output.model_dump(mode="json") == original


@pytest.mark.parametrize("failure_at", [None, "validation", "publication"])
def test_alias_adjustment_audit_survives_index_failures(trading, monkeypatch, failure_at):
    import asyncio
    import copy
    from unittest.mock import AsyncMock
    from alphapulse.news_research import service
    from alphapulse.news_research.contracts import IndexResult
    from alphapulse.news_research.models import Attempt, Package

    news = N(id=101, title="", summary="", content_text="插件调用单独计费", content_hash="hash",
             url="https://example.invalid/news", published_at=datetime.now(UTC))
    package = N(id=7, news_id=101, state="index_pending", content_hash="hash")
    output = IndexResult(keywords=[{"canonical": "插件计费", "aliases": ["插件调用", "插件调用收费"],
                                   "quote": "不存在的引文" if failure_at == "validation" else "插件调用单独计费"}])
    original = output.model_dump(mode="json")
    attempts, saved_rules = [], {}

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def add(self, row):
            row.id = 1
            attempts.append(row)
        async def commit(self):
            nonlocal saved_rules
            if attempts[0].state == "done" and failure_at == "publication":
                raise RuntimeError("发布写入失败")
            saved_rules = copy.deepcopy(attempts[0].rules)
        async def rollback(self):
            attempts[0].rules = copy.deepcopy(saved_rules)

    session = Session()
    session.get = AsyncMock(side_effect=lambda model, *args, **kwargs:
                            attempts[0] if model is Attempt else package if model is Package else news)
    session.execute = AsyncMock()
    monkeypatch.setattr(service, "SessionLocal", lambda: session)
    monkeypatch.setattr(service.vocab, "snapshot", AsyncMock(return_value={"entries": []}))
    monkeypatch.setattr(service, "model_config", AsyncMock(return_value=(None, "索引规则", {})))
    monkeypatch.setattr(service, "complete", AsyncMock(return_value=(output, {"input_tokens": 10})))

    asyncio.run(service._process_package(7, {}, "index"))
    attempt = attempts[0]
    assert attempt.state == ("failed" if failure_at else "done"), attempt.error
    if failure_at:
        assert {"validation": "原文引用", "publication": "发布写入失败"}[failure_at] in attempt.error
    assert attempt.rules["raw_output"] == original
    assert attempt.rules["output_adjustments"] == [{
        "path": "keywords.0.aliases", "original": ["插件调用", "插件调用收费"],
        "replacement": ["插件调用"], "reason": "aliases_absent_from_source"}]
    assert news.content_text == "插件调用单独计费" and output.model_dump(mode="json") == original


@pytest.mark.parametrize("task", ["index", "assign", "research"])
def test_model_config_sends_only_selected_task_and_records_exact_prompt(trading, monkeypatch, task):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research import service
    from alphapulse.news_research.contracts import INDEX_ASSIGN_SKILL, INDEX_SKILL, ASSIGN_SKILL, RESEARCH_SKILL
    from alphapulse.news_research.pipeline import BASE
    from alphapulse.models import SkillNode

    content = RESEARCH_SKILL if task == "research" else INDEX_ASSIGN_SKILL
    node = N(id=22, scope="news", kind="skill", enabled=True, parent_id=None, published_version_id=61)
    version = N(version_no=5, content=content)
    session = N(get=AsyncMock(side_effect=lambda model, *args: node if model is SkillNode else version))
    provider = N(id=12, model="test", max_tokens=1000, context_window=100000, reasoning_effort="medium")
    monkeypatch.setattr(service, "provider_for", AsyncMock(return_value=provider))
    config = {stage: dict(provider_id=12, model="test", skill_id=22) for stage in ("index", "research")}
    _, prompt, rules = asyncio.run(service.model_config(session, config, task))
    selected = {"index": INDEX_SKILL, "assign": ASSIGN_SKILL, "research": RESEARCH_SKILL}[task]
    assert prompt == BASE + "\n" + selected
    assert rules["system_prompt"] == prompt and rules["content"] == content
    assert rules["skill_id"] == 22 and rules["version"] == 5


@pytest.mark.parametrize("content", [
    "legacy unstructured rules",
    "## 索引规则\nonly one section",
    "## 索引规则\n\n## 归属规则\nassign",
    "## 索引规则\nindex\n## 索引规则\nagain",
    "preamble\n## 索引规则\nindex\n## 归属规则\nassign",
    "## 索引规则\nindex\n## 归属规则\nassign\n## 其他规则\nextra",
    "## 索引规则\nindex\n## 归属规则\nassign\n## 索引规则\nagain",
])
def test_invalid_first_stage_skill_fails_instead_of_sending_full_text(trading, content):
    from alphapulse.news_research.contracts import task_skill, SkillFormatError
    for task in ("index", "assign"):
        with pytest.raises(SkillFormatError):
            task_skill(content, task)


def test_task_skill_keeps_custom_rules_and_accepts_crlf(trading):
    from alphapulse.news_research.contracts import task_skill
    content = "## 归属规则\r\n自定义归属\r\n### 细则\r\n保留规则\r\n## 索引规则\r\n自定义索引"
    assert task_skill(content, "assign") == "自定义归属\n### 细则\n保留规则"
    assert task_skill(content, "index") == "自定义索引"
