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


@pytest.mark.parametrize('invalid', ['missing_source', 'unknown_reference', 'no_progress'])
def test_compaction_rejects_lost_provenance_and_stops_without_progress(pipeline, monkeypatch, invalid):
    provider = SimpleNamespace(max_tokens=100, context_window=8000)
    extracts = [{'facts': 'x'*3000, 'evidence_refs': [{'article_id': i, 'quote': 'original'}]} for i in (1, 2)]
    calls = []

    async def complete(provider, system, payload, schema):
        calls.append(payload)
        ids = [r['id'] for r in payload['evidence']]
        if invalid == 'missing_source': ids = ids[:1]
        if invalid == 'unknown_reference': ids[0] = 999
        facts = 'x'*10000 if invalid == 'no_progress' else 'compressed'
        return schema(facts=facts, evidence_ids=ids), {}

    monkeypatch.setattr(pipeline, 'complete', complete)
    message = {'no_progress': '未缩减输入', 'missing_source': '遗漏来源', 'unknown_reference': '未知或重复证据编号'}[invalid]
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
    articles = [{'id': i, 'title': f'来源{i}', 'summary': '原文摘要。', 'content': '完整原文。' * 5000} for i in (1, 2)]
    fragments = {i: {field: [] for field in ('title', 'summary', 'content')} for i in (1, 2)}
    scopes = [{'id': 10+i, 'news_id': i, 'scope_quote': f'来源{i}', 'other_scopes': []} for i in (1, 2)]

    async def complete(provider, rules, payload, schema):
        assert pipeline.fits_context(provider, rules, payload, schema)
        identifier = payload['article_id']
        assert payload['packages'] == [scopes[identifier-1]]
        for passage in payload['passages']:
            fragments[identifier][passage['field']].append(passage['text'])
        assert all(len(p['text']) <= 500 for p in payload['passages'])
        return schema(facts='保留事实。', evidence_ids=[0]), {'input_tokens': 10}

    monkeypatch.setattr(pipeline, 'complete', complete)
    extracts, usage = asyncio.run(pipeline.extract_articles(provider, '按原文提取事实。', articles, scopes=scopes))
    for article in articles:
        for field in ('title', 'summary', 'content'):
            assert ''.join(fragments[article['id']][field]) == article[field]
        assert len(fragments[article['id']]['content']) > 1
    assert len(usage) == len(extracts)
    assert {ref['article_id'] for part in extracts for ref in part['evidence_refs']} == {1, 2}
    pipeline.validate_evidence([pipeline.Evidence.model_validate(ref) for part in extracts for ref in part['evidence_refs']], {a['id']: a for a in articles})


@pytest.mark.parametrize('selection', [[999], [0, 0]])
def test_extraction_rejects_unknown_or_repeated_ids_and_retains_usage(pipeline, monkeypatch, selection):
    async def complete(provider, rules, payload, schema):
        return schema(facts='事实。', evidence_ids=selection), {'input_tokens': 17, 'output_tokens': 3}
    monkeypatch.setattr(pipeline, 'complete', complete)
    with pytest.raises(pipeline.SubmissionFailure, match='未知或重复证据编号') as failure:
        asyncio.run(pipeline.extract_articles(SimpleNamespace(max_tokens=100, context_window=12000), '规则',
            [{'id': 42, 'title': '原文标题', 'summary': '原文摘要', 'content': '原文正文'}]))
    assert failure.value.usage['parts'] == [{'input_tokens': 17, 'output_tokens': 3}]


def test_selected_quotes_preserve_fields_whitespace_and_foreign_language(pipeline, monkeypatch):
    article = {'id': 42, 'title': 'Official title', 'summary': 'Another field', 'content': 'Binance\n(SignalPlus) access was cut.\nNext report.'}
    async def complete(provider, rules, payload, schema):
        assert schema is pipeline.EvidenceSelection
        assert '不生成quote或article_id' in rules
        return schema(facts='币安切断访问。', evidence_ids=[p['id'] for p in payload['passages']]), {}
    monkeypatch.setattr(pipeline, 'complete', complete)
    extracts, _ = asyncio.run(pipeline.extract_articles(SimpleNamespace(max_tokens=100, context_window=12000), '规则', [article]))
    quotes = [r['quote'] for part in extracts for r in part['evidence_refs']]
    assert ''.join(quotes) == article['title']+article['summary']+article['content']
    assert all(any(quote in article[field] for field in ('title','summary','content')) for quote in quotes)
    with pytest.raises(ValueError, match='原文引用'):
        pipeline.validate_evidence([pipeline.Evidence(article_id=42, quote='Official title\nAnother field')], {42:article})


def test_extraction_schema_never_accepts_model_generated_quotes(pipeline):
    with pytest.raises(ValueError):
        pipeline.EvidenceSelection.model_validate({'facts':'事实。','evidence_ids':[0],
            'evidence_refs':[{'article_id':42,'quote':'编造引文'}]})


def test_single_long_sentence_splits_without_losing_characters(pipeline, monkeypatch):
    provider = SimpleNamespace(max_tokens=100, context_window=1500)
    article = {'id': 9, 'title': '', 'summary': '', 'content': '长' * 500}
    received = []
    async def complete(provider, rules, payload, schema):
        assert pipeline.fits_context(provider, rules, payload, schema)
        received.extend(p['text'] for p in payload['passages'])
        return schema(facts='分段事实。', evidence_ids=[0]), {}
    monkeypatch.setattr(pipeline, 'complete', complete)
    asyncio.run(pipeline.extract_articles(provider, '规则', [article]))
    assert len(received) > 1 and ''.join(received) == article['content']


@pytest.mark.parametrize('content', ['正文。\n\n', '   正文。   ', ' ' * 500 + '正文。' + '\n' * 500])
def test_whitespace_fragments_never_trigger_model_calls(pipeline, monkeypatch, content):
    article = {'id': 9, 'title': '标题。', 'summary': '\t\n', 'content': content}
    original = article.copy()
    passages = pipeline.source_passages(article)
    for field in ('title', 'summary', 'content'):
        assert ''.join(p['text'] for p in passages if p['field'] == field) == article[field]
    # Force every sentence and long-sentence fragment through the splitting path.
    monkeypatch.setattr(pipeline, 'fits_context', lambda _p, _s, payload, _schema:
        len(payload['passages']) == 1 and len(payload['passages'][0]['text']) <= 2)
    received = []

    async def complete(provider, rules, payload, schema):
        assert any(p['text'].strip() for p in payload['passages'])
        received.extend(payload['passages'])
        return schema(facts='保留新闻事实。', evidence_ids=[0]), {'input_tokens': 10}

    monkeypatch.setattr(pipeline, 'complete', complete)
    extracts, usage = asyncio.run(pipeline.extract_articles(SimpleNamespace(), '规则', [article]))
    assert len(usage) == len(extracts) == len(received)
    assert {ref['article_id'] for part in extracts for ref in part['evidence_refs']} == {9}
    for field in ('title', 'summary', 'content'):
        sent = ''.join(p['text'] for p in received if p['field'] == field)
        assert ''.join(sent.split()) == ''.join(article[field].split())
    assert article == original
    pipeline.validate_evidence([pipeline.Evidence.model_validate(ref) for part in extracts
        for ref in part['evidence_refs']], {9: article})


@pytest.mark.parametrize('content', ['', ' \n\t' * 500])
def test_unreadable_article_fails_without_model_calls(pipeline, monkeypatch, content):
    from unittest.mock import AsyncMock
    complete = AsyncMock()
    monkeypatch.setattr(pipeline, 'complete', complete)
    with pytest.raises(pipeline.SubmissionFailure, match='文章9.*缺少可读取原文'):
        asyncio.run(pipeline.extract_articles(SimpleNamespace(), '规则',
            [{'id': 9, 'title': '', 'summary': '', 'content': content}]))
    complete.assert_not_awaited()


def test_oversized_scope_fails_before_model_call_with_article_identity(pipeline, monkeypatch):
    from unittest.mock import AsyncMock
    complete = AsyncMock()
    monkeypatch.setattr(pipeline, 'complete', complete)
    with pytest.raises(pipeline.SubmissionFailure, match='文章9.*最小原文片段超出模型上下文'):
        asyncio.run(pipeline.extract_articles(SimpleNamespace(max_tokens=100, context_window=1500), '规则',
            [{'id': 9, 'title': '标题', 'content': '正文'}], scopes=[{'news_id': 9, 'scope_quote': '长' * 5000}]))
    complete.assert_not_awaited()
