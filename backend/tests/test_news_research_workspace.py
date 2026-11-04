"""Behavioral contracts for one-news packages and reusable keyword indexes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as N
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


def test_one_package_can_be_assigned_to_multiple_events_without_splitting(trading):
    from alphapulse.news_research.contracts import Assignment
    from alphapulse.news_research.service import validate_assignment

    base = {
        "new_key": None,
        "title": "Incident",
        "reason": "Follow-up",
        "quote": "Original",
        "change_kind": "new_fact",
        "urgent": False,
    }
    result = Assignment(targets=[{**base, "event_id": 1}, {**base, "event_id": 2}])
    validate_assignment(result, {"id": 8, "content": "Original"}, {1, 2})
    with pytest.raises(ValueError, match="未知事件"):
        validate_assignment(result, {"id": 8, "content": "Original"}, {1})
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


@pytest.mark.parametrize("state", ["failed", "pending", "done"])
def test_event_retry_preserves_content_revision_until_content_changes(trading, state):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.api import retry_event

    event = N(
        revision=7,
        state=state,
        dirty_since=None,
        due_at=datetime.now(UTC),
        error="Previous failure",
    )
    session = N(get=AsyncMock(return_value=event), commit=AsyncMock())
    asyncio.run(retry_event(1, session))
    assert event.revision == (8 if state == "done" else 7)
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
        (N(id=2, news_id=12, content_hash="current", keywords=[]), "current")
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


@pytest.mark.parametrize("status", ["unavailable", "no_matches", "matched"])
def test_retry_refreshes_unavailable_search_but_reuses_successful_results(
    trading, monkeypatch, status
):
    import asyncio
    from unittest.mock import AsyncMock
    from alphapulse.news_research.service import Research
    from alphapulse.news_risk import research as tools

    session = AsyncMock()
    session.__aenter__.return_value = session
    monkeypatch.setattr(tools, "SessionLocal", lambda: session)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = N(
        raise_for_status=lambda: None,
        json=lambda: {"results": [], "unresponsive_engines": []},
    )
    monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **kwargs: client)
    cached = {
        "status": status,
        "results": [{"url": "https://example.invalid/retry"}]
        if status == "matched"
        else [],
        "errors": [["engine", "CAPTCHA"]] if status == "unavailable" else [],
    }
    reader = Research(event={"id": 8})
    reader.restore(
        [{"tool": "search_web", "arguments": {"query": "same query"}, "result": cached}]
    )
    result = asyncio.run(reader.tool("search_web", {"query": "same query"}))
    if status == "unavailable":
        assert client.get.await_count == 1 and result["status"] == "no_matches"
        assert len(reader.records) == 1 and not reader.records[0].get("reused")
    else:
        assert client.get.await_count == 0 and result == cached
        assert reader.records[0]["reused"] is True


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
