"""Two-stage contracts reject ambiguous grouping and ungrounded conclusions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


@pytest.fixture
def pipeline(trading):
    from alphapulse.news_risk import pipeline
    return pipeline


def match(pipeline, **changes):
    return dict(event_id=None, group_key='event', title='公告', category='macro', entities=['央行'],
        fact='公告', evidence='公告', urgent=False, tags=['央行'], content_kind='report', **changes)


@pytest.mark.parametrize('event_id,group_key', [(None, None), (1, 'event')])
def test_grouping_target_is_exclusive(pipeline, event_id, group_key):
    data = match(pipeline)
    data.update(event_id=event_id, group_key=group_key)
    with pytest.raises(ValueError): pipeline.Match(**data)


def test_all_domains_require_nonempty_distinct_packages(pipeline):
    item = pipeline.Match(**match(pipeline))
    with pytest.raises(ValueError, match='重复关联'):
        pipeline.Grouping(reason='政治公告', events=[item, item])
    with pytest.raises(ValueError):
        pipeline.Grouping(reason='说明', events=[])
    assert pipeline.Grouping(reason='文化说明', events=[item]).events


@pytest.mark.parametrize('scope,enabled,published', [('shared', True, 1), ('news', False, 1), ('news', True, None)])
def test_unpublished_disabled_and_shared_skills_never_reach_model(pipeline, scope, enabled, published):
    node = SimpleNamespace(scope=scope, kind='skill', published_version_id=published, enabled=enabled, parent_id=None)
    session = SimpleNamespace(get=AsyncMock(return_value=node))
    with pytest.raises(ValueError):
        asyncio.run(pipeline.configuration(session, SimpleNamespace(grouping_skill_id=1), 'grouping'))


def test_evidence_cannot_cross_article_boundaries(pipeline):
    articles = {1: {'title': '央行维持利率'}, 2: {'title': '交易所暂停提现'}}
    with pytest.raises(ValueError, match='原文引用'):
        pipeline.validate_evidence([pipeline.Evidence(article_id=1, quote='交易所暂停提现')], articles)
    pipeline.validate_evidence([pipeline.Evidence(article_id=2, quote='暂停提现')], articles)


def test_old_backfill_is_not_selected_by_ingestion_time(pipeline):
    from datetime import UTC, datetime
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import dialect
    from alphapulse.models import NewsItem
    from alphapulse.news_risk.worker import eligible
    sql = str(select(NewsItem.id).where(eligible(datetime.now(UTC))).compile(dialect=dialect()))
    assert 'greatest(news_items.published_at, news_items.source_updated_at)' in sql
    assert 'first_seen_at' not in sql and 'last_seen_at' not in sql


def event_result(pipeline, refs):
    return pipeline.EventResult(id=1, title='Synthetic event', summary='Synthetic summary', category='macro', entities=[],
        risk_stars=1, importance_stars=4, direction='unknown', verification='insufficient', confidence='low', reasoning='Synthetic', uncertainty='Synthetic',
        facts='Synthetic', timeline='Synthetic', asset_impacts='Unknown', opinions='Unknown', counter_evidence='Unknown', watchpoints='Unknown', claims=[pipeline.Claim(statement='Synthetic', verdict='insufficient', evidence_refs=[])], related_packages=[], excluded_article_ids=[], evidence_refs=refs)


def test_long_event_uses_bounded_layers_and_retains_all_sources(pipeline, monkeypatch):
    provider = SimpleNamespace(max_tokens=2000, context_window=12000)
    articles = [{'id': i+1, 'title': 'Synthetic', 'summary': '', 'content': 'b'*20000} for i in range(20)]
    calls = []

    async def complete(provider, system, payload, schema):
        assert pipeline.fits_context(provider, system, payload, schema)
        calls.append(payload['task'])
        if payload['task'] == 'extract':
            return pipeline.Extraction(facts='x'*600, evidence_refs=[pipeline.Evidence(
                article_id=payload['article_id'], quote=payload['content'].strip()[0])]), {}
        refs = {r['article_id']: r for e in payload['extractions'] for r in e['evidence_refs']}
        if payload['task'] == 'consolidate':
            return pipeline.Extraction(facts='Consolidated facts and conflicts', evidence_refs=list(refs.values())), {}
        assert set(refs) == {a['id'] for a in articles}
        return event_result(pipeline, list(refs.values())), {}

    monkeypatch.setattr(pipeline, 'complete', complete)
    result, usage = asyncio.run(pipeline.summarize_articles(provider, 'rules', articles, {'id': 1, 'fact': 'Synthetic event'}))
    assert result.id == 1 and calls[-1] == 'event'
    assert calls.count('extract') > len(articles) and calls.count('consolidate') > 1
    assert len(usage['parts']) == len(calls)-1


@pytest.mark.parametrize('invalid', ['missing_source', 'rewritten_quote', 'no_progress'])
def test_compaction_rejects_lost_provenance_and_stops_without_progress(pipeline, monkeypatch, invalid):
    provider = SimpleNamespace(max_tokens=100, context_window=8000)
    extracts = [{'facts': 'x'*3000, 'evidence_refs': [{'article_id': i, 'quote': 'original'}]} for i in (1, 2)]
    calls = []

    async def complete(provider, system, payload, schema):
        calls.append(payload)
        refs = [r for e in payload['extractions'] for r in e['evidence_refs']]
        if invalid == 'missing_source': refs = refs[:1]
        if invalid == 'rewritten_quote': refs[0] = {**refs[0], 'quote': 'invented'}
        facts = 'x'*10000 if invalid == 'no_progress' else 'compressed'
        return pipeline.Extraction(facts=facts, evidence_refs=refs), {}

    monkeypatch.setattr(pipeline, 'complete', complete)
    message = '未缩减输入' if invalid == 'no_progress' else '遗漏来源或改写'
    with pytest.raises(ValueError, match=message):
        asyncio.run(pipeline.compact_extractions(provider, 'rules', {'task': 'event', 'id': 1, 'extractions': extracts}, pipeline.EventResult))
    assert len(calls) == 1


def test_large_candidate_catalog_does_not_extract_or_drop_short_article(pipeline, monkeypatch):
    provider = SimpleNamespace(max_tokens=2000, context_window=12000)
    article = {'id': 1, 'title': 'Synthetic', 'summary': '', 'content': 'Original text'}
    candidates = [{'id': i+1, 'title': 'x'*200, 'published_at': '2026-10-31T00:00:00Z', 'entities': ['entity']} for i in range(80)]
    complete = AsyncMock(side_effect=AssertionError('Candidate overflow must not trigger source extraction'))
    monkeypatch.setattr(pipeline, 'complete', complete)
    payload, usage = asyncio.run(pipeline.grouping_payload(provider, 'rules', article, candidates))
    assert payload['article'] == article and not usage
    assert 0 < len(payload['candidates']) < len(candidates)
    assert payload['candidates'] == candidates[:len(payload['candidates'])]
    assert pipeline.fits_context(provider, 'rules', payload, pipeline.Grouping)
    corrected = pipeline.pack_candidates(provider, 'rules', {**payload, 'correction': 'Invalid output '*100}, payload['candidates'])
    assert pipeline.fits_context(provider, 'rules', corrected, pipeline.Grouping)
    assert len(corrected['candidates']) < len(payload['candidates'])
    complete.assert_not_awaited()


def test_long_grouping_source_is_compacted_before_candidates(pipeline, monkeypatch):
    provider = SimpleNamespace(max_tokens=2000, context_window=12000)
    article = {'id': 1, 'title': 'Synthetic', 'summary': '', 'content': 'b'*200000}
    calls = []

    async def complete(provider, system, payload, schema):
        assert pipeline.fits_context(provider, system, payload, schema)
        calls.append(payload['task'])
        if payload['task'] == 'extract':
            return pipeline.Extraction(facts='x'*600, evidence_refs=[pipeline.Evidence(article_id=1, quote='b')]), {}
        return pipeline.Extraction(facts='Compressed facts', evidence_refs=[pipeline.Evidence(article_id=1, quote='b')]), {}

    monkeypatch.setattr(pipeline, 'complete', complete)
    payload, _ = asyncio.run(pipeline.grouping_payload(provider, 'rules', article, [{'id': 2, 'title': 'Candidate'}]))
    assert 'consolidate' in calls
    assert payload['article_id'] == 1 and payload['candidates'][0]['id'] == 2
    assert pipeline.fits_context(provider, 'rules', payload, pipeline.Grouping)


@pytest.mark.parametrize('length', [50, 20000])
def test_package_scope_survives_direct_and_extracted_roundup(pipeline, monkeypatch, length):
    provider = SimpleNamespace(max_tokens=2000, context_window=12000)
    article = {'id': 1, 'title': 'Roundup', 'summary': '', 'content': 'b'*length}
    observed = []
    async def complete(provider, system, payload, schema):
        observed.append(payload)
        if schema is pipeline.Extraction:
            return pipeline.Extraction(facts=payload['package']['fact'], evidence_refs=[pipeline.Evidence(article_id=1, quote='b')]), {}
        return event_result(pipeline, [pipeline.Evidence(article_id=1, quote='b')]).model_copy(update={'id': payload['id']}), {}
    monkeypatch.setattr(pipeline, 'complete', complete)
    for identifier, fact in [(1, '央行公布利率'), (2, '公司发布新产品')]:
        package = {'id': identifier, 'title': fact, 'fact': fact, 'tags': [fact], 'category': 'general', 'content_kind': 'report'}
        before = len(observed)
        result, _ = asyncio.run(pipeline.summarize_articles(provider, 'rules', [article], package))
        assert result.id == identifier
        assert all(payload['package'] == package for payload in observed[before:])
    assert observed[-1]['package']['fact'] == '公司发布新产品'


def test_long_event_reserves_real_research_tools_before_extraction(pipeline, monkeypatch):
    from alphapulse.news_risk import research
    provider = SimpleNamespace(max_tokens=1000, context_window=12000, reasoning_effort=None)
    article = {'id': 1, 'title': 'Report', 'summary': '', 'content': ''}
    package = {'id': 1, 'fact': '单个事实'}
    while pipeline.fits_context(provider, 'rules', {'task': 'event', 'id': 1, 'package': package,
            'articles': [{**article, 'content': article['content']+'b'*100}]}, pipeline.EventResult):
        article['content'] += 'b'*100
    review = research.Research()
    assert not review.fits(provider, 'rules', {'task': 'event', 'id': 1, 'package': package, 'articles': [article]}, pipeline.EventResult)
    extracted = []
    async def complete(provider, system, payload, schema):
        extracted.append(payload)
        return pipeline.Extraction(facts='原文中的事实', evidence_refs=[pipeline.Evidence(article_id=1, quote='b')]), {}
    async def stream(provider, system, messages, tools, effort):
        assert review.fits_messages(provider, system, messages, pipeline.EventResult)
        assert 'extractions' in messages[0]['text']
        result = event_result(pipeline, [pipeline.Evidence(article_id=1, quote='b')])
        yield research.llm.Completion(tool_calls=[{'id': 'done', 'name': 'submit', 'arguments': result.model_dump()}])
    monkeypatch.setattr(pipeline, 'complete', complete)
    monkeypatch.setattr(research.llm, 'stream', stream)
    result, _ = asyncio.run(pipeline.summarize_articles(provider, 'rules', [article], package, review))
    assert result.id == 1 and extracted
