"""Two-stage contracts reject ambiguous grouping and ungrounded conclusions."""
import asyncio
from types import SimpleNamespace
import pytest


@pytest.fixture
def pipeline(trading):
    from alphapulse.news_research import pipeline
    return pipeline


def test_evidence_cannot_cross_article_boundaries(pipeline):
    articles = {1: {'title': '央行维持利率'}, 2: {'title': '交易所暂停提现'}}
    with pytest.raises(ValueError, match='原文引用'):
        pipeline.validate_evidence([pipeline.Evidence(article_id=1, quote='交易所暂停提现')], articles)
    pipeline.validate_evidence([pipeline.Evidence(article_id=2, quote='暂停提现')], articles)


def event_result(pipeline, refs):
    return pipeline.ReviewResult(id=1, title='Synthetic event', summary='Synthetic summary', category='macro', entities=[],
        risk_stars=1, importance_stars=4, direction='unknown', verification='insufficient', confidence='low', reasoning='Synthetic', uncertainty='Synthetic',
        timeline_points=[dict(occurred_at=None, time_precision='unknown', time_text='Unknown', reported_at=None, description='Synthetic', verification='insufficient', evidence_refs=[pipeline.Evidence.model_validate(r).model_dump() for r in refs])], facts='Synthetic', timeline='Synthetic', asset_impacts='Unknown', opinions='Unknown', counter_evidence='Unknown', watchpoints='Unknown', claims=[pipeline.Claim(statement='Synthetic', verdict='insufficient', evidence_refs=[])], excluded_article_ids=[], evidence_refs=refs)


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
        asyncio.run(pipeline.compact_extractions(provider, 'rules', {'task': 'event', 'id': 1, 'extractions': extracts}, pipeline.ReviewResult))
    assert len(calls) == 1


def test_extraction_requires_original_evidence(pipeline, monkeypatch):
    async def complete(provider, system, payload, schema):
        return schema.model_validate({'facts': '', 'evidence_refs': []}), {}
    monkeypatch.setattr(pipeline, 'complete', complete)
    with pytest.raises(ValueError):
        asyncio.run(pipeline.extract_articles(SimpleNamespace(max_tokens=100, context_window=12000), 'rules',
            [{'id': 1, 'title': 'Original', 'content': '', 'summary': ''}]))
    with pytest.raises(ValueError):
        event_result(pipeline, [])


def test_long_extraction_preserves_complete_originals_and_source_ids(pipeline, monkeypatch):
    provider = SimpleNamespace(max_tokens=1000, context_window=12000)
    articles = [{'id': i, 'title': f'来源{i}', 'summary': '', 'content': '完整原文。' * 5000} for i in (1, 2)]
    fragments = {1: [], 2: []}

    async def complete(provider, rules, payload, schema):
        assert pipeline.fits_context(provider, rules, payload, schema)
        identifier, content = payload['article_id'], payload['content']
        fragments[identifier].append(content)
        return pipeline.Extraction(facts='保留事实。', evidence_refs=[
            pipeline.Evidence(article_id=identifier, quote='完整原文。')]), {'input_tokens': 10}

    monkeypatch.setattr(pipeline, 'complete', complete)
    extracts, usage = asyncio.run(pipeline.extract_articles(provider, '按原文提取事实。', articles))
    for article in articles:
        assert ''.join(fragments[article['id']]) == article['title']+'\n\n'+article['content']
        assert len(fragments[article['id']]) > 1
    assert len(usage) == len(extracts) == sum(len(parts) for parts in fragments.values())
    assert {ref['article_id'] for part in extracts for ref in part['evidence_refs']} == {1, 2}
