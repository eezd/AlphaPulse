"""Research and briefing acceptance uses synthetic I/O only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def test_private_and_mixed_dns_are_rejected(trading, monkeypatch):
    from alphapulse.news_risk.research import public_address
    async def check():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[(None, None, None, None, ('127.0.0.1', 80))]))
        with pytest.raises(ValueError, match='非公开'): await public_address('https://example.invalid/test')
        monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[(None, None, None, None, ('8.8.8.8', 80)), (None, None, None, None, ('10.0.0.1', 80))]))
        with pytest.raises(ValueError, match='非公开'): await public_address('https://example.invalid/test')
        for url in ('file:///etc/passwd', 'http://user:password@example.invalid', 'http://example.invalid:8000'):
            with pytest.raises(ValueError): await public_address(url)
    asyncio.run(check())


@pytest.mark.parametrize('discovered', [False, True])
def test_read_web_supports_direct_urls_and_budget(trading, monkeypatch, discovered):
    from alphapulse.news_risk import research as module
    reader = module.Research()
    url = 'https://example.invalid/official'
    if discovered:
        reader.urls.add(url)
    fetch = AsyncMock(return_value={'url': url, 'content': 'Official original'})
    monkeypatch.setattr(module, 'fetch_public', fetch)
    result = asyncio.run(reader.tool('read_web', {'url': url}))
    assert result['discovery_method'] == ('discovered' if discovered else 'direct')
    assert result['article_id'] < 0
    fetch.assert_awaited_once_with(url)
    reader.counts['read_web'] = 4
    with pytest.raises(ValueError, match='预算'):
        asyncio.run(reader.tool('read_web', {'url': url + '/other'}))
    assert fetch.await_count == 1


def test_briefing_covers_all_packages_across_batches(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    provider = SimpleNamespace(max_tokens=100, context_window=7000)
    inputs = [{'id': i, 'revision': 2, 'importance_stars': 3, 'result': {'summary': 'x'*1600}} for i in range(15)]
    batches = []
    async def complete(provider, system, payload, schema):
        assert briefing.fits_context(provider, system, payload, schema)
        if schema is briefing.Ranked:
            batches.append(payload['packages'])
            return briefing.Ranked(reading=[briefing.Reading(id=p['id'], revision=2, priority=p['id'], reason='Read') for p in payload['packages']], notes='Notes'), {}
        trend = briefing.Trend(summary='Unknown', conditions='Unknown', counter_evidence='Unknown', watchpoints='Unknown')
        return briefing.Synthesis(summary='Unknown', market='Unknown', short_term=trend, medium_term=trend, references=[]), {}
    monkeypatch.setattr(briefing, 'complete', complete)
    result = asyncio.run(briefing.generate(provider, 'rules', inputs))
    assert len(batches) > 1
    assert [p['id'] for p in result['reading']] == list(reversed(range(15)))


@pytest.mark.parametrize('ids', [[], [1, 1], [2]])
def test_briefing_rejects_missing_duplicate_and_unknown_ids(trading, monkeypatch, ids):
    from alphapulse.news_risk import briefing
    async def complete(*args):
        return briefing.Ranked(reading=[briefing.Reading(id=i, revision=1, priority=50, reason='Read') for i in ids], notes='Notes'), {}
    monkeypatch.setattr(briefing, 'complete', complete)
    with pytest.raises(ValueError, match='遗漏、重复'):
        asyncio.run(briefing.generate(SimpleNamespace(max_tokens=100, context_window=10000), 'rules', [{'id': 1, 'revision': 1, 'importance_stars': 5}]))


def test_high_importance_positive_package_is_valid(trading):
    from alphapulse.news_risk import pipeline
    from test_news_pipeline import event_result
    result = event_result(pipeline, [pipeline.Evidence(article_id=1, quote='突破')])
    result.risk_stars = 0; result.importance_stars = 5
    pipeline.validate_event(result, {1: {'title': '科技突破'}})
    assert result.importance_stars == 5 and result.risk_stars == 0


def test_public_fetch_pins_address_and_rechecks_redirect(trading, monkeypatch):
    import httpx
    from alphapulse.news_risk import research
    calls = []
    original = httpx.AsyncClient
    async def resolve(url):
        from urllib.parse import urlsplit
        if 'private.invalid' in url: raise ValueError('非公开地址')
        return urlsplit(url), '8.8.8.8'
    async def handle(request):
        calls.append(request)
        assert request.url.host == '8.8.8.8' and request.headers['host'] == 'public.invalid'
        assert request.extensions['sni_hostname'] == 'public.invalid'
        return httpx.Response(302, headers={'location': 'http://private.invalid'})
    monkeypatch.setattr(research, 'public_address', resolve)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    with pytest.raises(ValueError, match='非公开'):
        asyncio.run(research.fetch_public('https://public.invalid'))
    assert len(calls) == 1


def test_failed_review_can_reuse_saved_evidence_without_new_io(trading):
    from alphapulse.news_risk.research import Research
    research = Research()
    record = {'tool': 'read_web', 'arguments': {'url': 'https://example.invalid'}, 'result': {'article_id': -1, 'content': 'Original'}}
    research.restore([record])
    assert asyncio.run(research.tool('read_web', record['arguments']))['content'] == 'Original'
    assert not research.records and research.counts['read_web'] == 0


def test_restored_news_is_sent_before_reusing_its_reference(trading, monkeypatch):
    import json
    from pydantic import BaseModel
    from alphapulse.news_risk import research
    class Result(BaseModel):
        summary: str
    review = research.Research({'id': 42})
    review.restore([{'tool': 'read_news', 'arguments': {'id': 123},
        'result': {'article_id': -1, 'news_id': 123, 'content': 'Restored original'}}])
    rounds = []
    async def stream(provider, system, messages, tools, effort):
        rounds.append(len(messages))
        if len(rounds) == 2:
            assert json.loads(messages[-1]['result'])['content'] == 'Restored original'
        if len(rounds) == 3:
            reply = json.loads(messages[-1]['result'])
            assert reply['article_id'] == -1 and reply['status'] == 'already_provided'
            assert 'content' not in reply
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit',
                'arguments': {'summary': 'Restored original'}}])
        else:
            yield research.llm.Completion(tool_calls=[{'id': str(len(rounds)), 'name': 'read_news', 'arguments': {'id': 123}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    result, _ = asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None),
        'rules', {'articles': []}, Result))
    assert result.summary == 'Restored original' and len(rounds) == 3
    assert review.counts['read'] == 0 and not review.records


def test_current_input_takes_precedence_over_restored_news(trading, monkeypatch):
    from pydantic import BaseModel
    from alphapulse.news_risk import research
    class Result(BaseModel):
        summary: str
    review = research.Research()
    review.restore([{'tool': 'read_news', 'arguments': {'id': 123},
        'result': {'article_id': -1, 'news_id': 123, 'content': 'Old original'}}])
    async def stream(provider, system, messages, tools, effort):
        if len(messages) == 1:
            yield research.llm.Completion(tool_calls=[{'id': 'read', 'name': 'read_news', 'arguments': {'id': 123}}])
        else:
            import json
            reply = json.loads(messages[-1]['result'])
            assert reply['article_id'] == 9 and reply['status'] == 'already_provided'
            assert 'Old original' not in json.dumps(messages)
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit', 'arguments': {'summary': 'Current original'}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None), 'rules',
        {'articles': [{'id': 9, 'news_id': 123, 'content': 'Current original'}]}, Result))
    assert review.counts['read'] == 0


def test_research_stops_after_eight_tool_rounds_and_retains_failures(trading, monkeypatch):
    from alphapulse.news_risk import research
    from pydantic import BaseModel
    class Result(BaseModel):
        summary: str
    rounds = []
    async def stream(provider, system, messages, tools, effort):
        rounds.append(tools)
        if len(rounds) == 9:
            assert [tool.name for tool in tools] == ['submit']
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit', 'arguments': {'summary': '证据不足'}}])
        else:
            yield research.llm.Completion(tool_calls=[{'id': str(len(rounds)), 'name': 'unknown_tool', 'arguments': {}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    review = research.Research()
    result, _ = asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None), 'rules', {}, Result))
    assert result.summary == '证据不足'
    assert len(rounds) == 9 and len(review.records) == 8
    assert all('error' in record['result'] for record in review.records)


@pytest.mark.parametrize('mode,created', [('unchanged', False), ('ordinary_wait', False), ('ordinary_due', True), ('urgent_wait', False), ('urgent_due', True), ('expired', True)])
def test_reading_updates_only_for_changes_and_respects_aggregation(trading, monkeypatch, mode, created):
    from datetime import UTC, datetime, timedelta
    from alphapulse.news_risk import worker, events
    now = datetime.now(UTC)
    event = SimpleNamespace(id=1, published_revision=2, importance_stars=5 if mode.startswith('urgent') else 3,
        category='politics', tags=['政策'], result={'summary': '当前说明'}, analyzed_at=now)
    semantic = [{'kind': 'package', 'id': event.id, 'importance_stars': event.importance_stars, 'result': event.result}]
    fingerprint = worker.briefing.fingerprint(semantic, {}) if mode == 'unchanged' else 'previous'
    latest = SimpleNamespace(created_at=now-timedelta(seconds=30 if mode == 'urgent_wait' else 120), quotes={'fingerprint': fingerprint})
    if mode in ('ordinary_due', 'expired'): latest.quotes['dirty_since'] = (now-timedelta(minutes=16)).isoformat()
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.return_value = SimpleNamespace(enabled=True)
    session.scalar.side_effect = [latest, None, None] + ([None] if mode == 'expired' else [])
    session.scalars.return_value = SimpleNamespace(all=lambda: [] if mode == 'expired' else [event])
    monkeypatch.setattr(events, 'reading_inputs', AsyncMock(return_value=[] if mode == 'expired' else [{'kind': 'package', 'id': event.id, 'revision': event.published_revision, 'importance_stars': event.importance_stars, 'category': event.category, 'tags': event.tags, 'result': event.result}]))
    added = []
    session.add = added.append
    monkeypatch.setattr(worker, 'SessionLocal', lambda: session)
    monkeypatch.setattr(worker, 'configuration', AsyncMock(return_value=(SimpleNamespace(model='advanced'), 'rules', {}, 'rules-key')))
    monkeypatch.setattr(worker.briefing, 'generate', AsyncMock(return_value={'reading': [], 'synthesis': None, 'usage': []}))
    asyncio.run(worker.outlook())
    assert bool(added) is created
    if created: assert added[0].state == 'done'
    if mode == 'ordinary_wait': assert latest.quotes['dirty_since']


def test_truncated_reading_batches_split_until_all_versions_are_ranked(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    from alphapulse.news_risk.service import OutputTruncated
    inputs = [{'id': i, 'revision': 3, 'importance_stars': 3, 'result': {'summary': 'Short'}} for i in range(1, 41)]
    calls = []
    async def complete(provider, system, payload, schema):
        calls.append(payload)
        if schema is briefing.Ranked:
            if len(payload['packages']) > 5: raise OutputTruncated('Synthetic output limit', {'output_tokens': 256})
            return briefing.Ranked(reading=[briefing.Reading(id=p['id'], revision=3, priority=100-p['id'], reason='Read') for p in payload['packages']], notes='Notes'), {'output_tokens': 100}
        trend = briefing.Trend(summary='Unknown', conditions='Unknown', counter_evidence='Unknown', watchpoints='Unknown')
        return briefing.Synthesis(summary='Unknown', market='Unknown', short_term=trend, medium_term=trend, references=[]), {}
    monkeypatch.setattr(briefing, 'complete', complete)
    result = asyncio.run(briefing.generate(SimpleNamespace(max_tokens=256, context_window=200000), 'rules', inputs))
    assert [r['id'] for r in result['reading']] == list(range(1, 41))
    assert all(r['revision'] == 3 for r in result['reading'])
    assert len(result['usage']) == len(calls)
    assert any(u.get('output_tokens') == 256 for u in result['usage'])


def test_single_package_truncation_is_bounded_and_does_not_retry_other_errors(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    from alphapulse.news_risk.service import OutputTruncated
    complete = AsyncMock(side_effect=OutputTruncated('Synthetic output limit'))
    monkeypatch.setattr(briefing, 'complete', complete)
    with pytest.raises(OutputTruncated, match='单包排序'):
        asyncio.run(briefing.generate(SimpleNamespace(max_tokens=256, context_window=200000), 'rules', [{'id': 1, 'revision': 1, 'importance_stars': 3}]))
    assert complete.await_count == 1
    complete = AsyncMock(side_effect=ValueError('Malformed ranking'))
    monkeypatch.setattr(briefing, 'complete', complete)
    with pytest.raises(ValueError, match='Malformed'):
        asyncio.run(briefing.generate(SimpleNamespace(max_tokens=256, context_window=200000), 'rules', [{'id': 1, 'revision': 1}, {'id': 2, 'revision': 1}]))
    assert complete.await_count == 1


@pytest.mark.parametrize('split', [False, True])
def test_terminal_truncation_keeps_completed_and_split_attempt_usage(trading, monkeypatch, split):
    from alphapulse.news_risk import briefing
    from alphapulse.news_risk.service import OutputTruncated
    calls = []
    async def complete(provider, system, payload, schema):
        calls.append(payload)
        if split and len(calls) == 1:
            raise OutputTruncated('Split batch', {'output_tokens': 30})
        if len(calls) == (2 if split else 1):
            return briefing.Ranked(reading=[briefing.Reading(**p, priority=50, reason='Read') for p in payload['expected']],
                notes='Notes'), {'output_tokens': 120}
        raise OutputTruncated('Single item', {'output_tokens': 20})
    monkeypatch.setattr(briefing, 'complete', complete)
    inputs = [{'id': i, 'revision': 1, 'importance_stars': 3} for i in range(2 if split else 13)]
    with pytest.raises(OutputTruncated, match='单包排序') as error:
        asyncio.run(briefing.generate(SimpleNamespace(max_tokens=1000, context_window=200000), 'rules', inputs, ranking_only=True))
    assert [u['output_tokens'] for u in error.value.usage['rounds']] == ([30, 120, 20] if split else [120, 20])
    assert len(calls) == (3 if split else 2)


@pytest.mark.parametrize('change,expected', [('links', False), ('verdict', True), ('risk', True), ('fact', True)])
def test_reposts_do_not_trigger_advanced_calls_but_review_changes_do(trading, monkeypatch, change, expected):
    from copy import deepcopy
    from datetime import UTC, datetime, timedelta
    from alphapulse.news_risk import worker, briefing
    now = datetime.now(UTC)
    inputs = [{'id': 1, 'revision': 1, 'importance_stars': 5, 'result': {'facts': '公开说明', 'risk_stars': 1,
        'evidence_refs': [{'article_id': 1, 'quote': '公开说明'}],
        'claims': [{'statement': '公开说明', 'verdict': 'partial', 'evidence_refs': [{'article_id': 1, 'quote': '公开说明'}]}]}}]
    semantic = briefing.semantic_packages(inputs)
    latest = SimpleNamespace(created_at=now-timedelta(minutes=2), quotes={'fingerprint': briefing.fingerprint(semantic, {}),
        'important': {'1': briefing.fingerprint(semantic[0], {})}})
    updated = deepcopy(inputs)
    updated[0]['revision'] = 2
    updated[0]['result']['evidence_refs'].append({'article_id': 2, 'quote': '公开说明'})
    updated[0]['result']['claims'][0]['evidence_refs'].append({'article_id': 2, 'quote': '公开说明'})
    if change == 'verdict': updated[0]['result']['claims'][0]['verdict'] = 'supported'
    if change == 'risk': updated[0]['result']['risk_stars'] = 5
    if change == 'fact': updated[0]['result']['facts'] = '官方修订说明'
    assert (briefing.semantic_packages(updated) != semantic) is expected
    session = AsyncMock(); session.__aenter__.return_value = session
    session.get.return_value = SimpleNamespace(enabled=True); session.scalar.side_effect = [latest, None, None]
    session.scalars.return_value = SimpleNamespace(all=lambda: [SimpleNamespace(id=1, published_revision=2,
        importance_stars=5, category='politics', tags=[], result=updated[0]['result'], analyzed_at=now)])
    from alphapulse.news_risk import events
    monkeypatch.setattr(events, 'reading_inputs', AsyncMock(return_value=[{**p, 'kind': 'package', 'category': 'politics', 'tags': []} for p in updated]))
    session.add = lambda value: None
    generated = AsyncMock(return_value={'reading': [], 'synthesis': None, 'usage': []})
    monkeypatch.setattr(worker, 'SessionLocal', lambda: session)
    monkeypatch.setattr(worker, 'configuration', AsyncMock(return_value=(SimpleNamespace(model='advanced'), 'rules', {}, 'key')))
    monkeypatch.setattr(briefing, 'generate', generated)
    asyncio.run(worker.outlook())
    assert bool(generated.await_count) is expected


def test_current_package_read_needs_no_io_or_budget(trading):
    from alphapulse.news_risk.research import Research
    research = Research({'id': 42, 'revision': 3})
    result = asyncio.run(research.tool('read_package', {'id': 42}))
    assert result['status'] == 'already_provided' and result['revision'] == 3
    assert research.counts['read'] == 0 and not research.records


def test_provided_original_is_reused_without_io(trading, monkeypatch):
    from alphapulse.news_risk import research
    from pydantic import BaseModel
    class Result(BaseModel):
        summary: str
    async def stream(provider, system, messages, tools, effort):
        if len(messages) == 1:
            yield research.llm.Completion(tool_calls=[{'id': 'read', 'name': 'read_news', 'arguments': {'id': 123}}])
        else:
            import json
            response = json.loads(messages[-1]['result'])
            assert response['article_id'] == 9 and 'content' not in response
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit', 'arguments': {'summary': 'Done'}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    review = research.Research()
    asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None), '',
        {'articles': [{'id': 9, 'news_id': 123, 'content': 'Original'}]}, Result))
    assert review.sources[9]['content'] == 'Original'
    assert review.counts['read'] == 0 and not review.records


def test_web_unavailable_short_circuits_only_this_review(trading, monkeypatch):
    from alphapulse.news_risk import research
    session = AsyncMock()
    session.__aenter__.return_value = session
    monkeypatch.setattr(research, 'SessionLocal', lambda: session)
    client = AsyncMock()
    client.__aenter__.return_value = client
    response = SimpleNamespace(raise_for_status=lambda: None,
        json=lambda: {'results': [], 'unresponsive_engines': [['engine', 'CAPTCHA']]})
    client.get.return_value = response
    monkeypatch.setattr(research.httpx, 'AsyncClient', lambda **kwargs: client)
    review = research.Research()
    assert asyncio.run(review.tool('search_web', {'query': 'first query'}))['status'] == 'unavailable'
    assert asyncio.run(review.tool('search_web', {'query': 'different query'}))['status'] == 'unavailable'
    assert client.get.await_count == 1 and review.counts['search_web'] == 1
    assert research.Research().web_unavailable is None


def test_ranking_repairs_identity_once_and_accounts_usage(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    calls = []
    async def complete(provider, system, payload, schema):
        calls.append(payload)
        ids = [] if len(calls) == 1 else [p['id'] for p in payload['packages']]
        return briefing.Ranked(reading=[briefing.Reading(id=i, revision=2, priority=50, reason='Read') for i in ids], notes='Notes'), {'output_tokens': 10}
    monkeypatch.setattr(briefing, 'complete', complete)
    result = asyncio.run(briefing.generate(SimpleNamespace(max_tokens=100, context_window=10000), '',
        [{'id': 1, 'revision': 2, 'importance_stars': 3}], ranking_only=True))
    assert len(calls) == 2 and calls[1]['correction']['errors']['missing'] == [('package', 1, 2)]
    assert len(result['usage']) == 2 and result['reading'][0]['id'] == 1


def test_ranking_missing_revision_repaired_with_explicit_identity(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    from alphapulse.news_risk.service import SubmissionFailure
    from pydantic import ValidationError
    calls = []
    async def complete(provider, system, payload, schema):
        calls.append(payload)
        if len(calls) == 1:
            try: briefing.Ranked.model_validate({'reading': [{'id': 1, 'priority': 50, 'reason': 'Read'}], 'notes': 'Notes'})
            except ValidationError as error: raise SubmissionFailure('Missing revision', {'output_tokens': 10}) from error
        return briefing.Ranked(reading=[briefing.Reading(**payload['expected'][0], priority=50, reason='Read')], notes='Notes'), {}
    monkeypatch.setattr(briefing, 'complete', complete)
    result = asyncio.run(briefing.generate(SimpleNamespace(max_tokens=100, context_window=10000), '',
        [{'id': 1, 'revision': 2, 'importance_stars': 3}], ranking_only=True))
    assert len(calls) == 2 and len(result['usage']) == 2


def test_ranking_batches_are_bounded_and_cover_every_identity(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    counts = []
    async def complete(provider, system, payload, schema):
        counts.append(len(payload['packages']))
        return briefing.Ranked(reading=[briefing.Reading(**p, priority=50, reason='Read') for p in payload['expected']], notes='Notes'), {}
    monkeypatch.setattr(briefing, 'complete', complete)
    result = asyncio.run(briefing.generate(SimpleNamespace(max_tokens=1000, context_window=200000), '',
        [{'id': i, 'revision': 1, 'importance_stars': 3} for i in range(25)], ranking_only=True))
    assert counts == [12, 12, 1] and len(result['reading']) == 25


def test_package_search_excludes_current_package(trading, monkeypatch):
    from alphapulse.news_risk import research
    from sqlalchemy.dialects.postgresql import dialect
    statements = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def scalars(self, statement):
            statements.append(statement.compile(dialect=dialect()))
            return SimpleNamespace(all=lambda: [])
    monkeypatch.setattr(research, 'SessionLocal', Session)
    review = research.Research({'id': 42})
    asyncio.run(review.tool('search_packages', {'query': 'policy', 'keywords': ['policy']}))
    assert 'news_ai_summaries.id !=' in str(statements[0]) and 42 in statements[0].params.values()


@pytest.mark.parametrize('result,expected', [({'title': '媒体称11月2日拟开会'}, '媒体称11月2日拟开会'), (None, '明天开会')])
def test_package_api_returns_fact_title_without_legacy_rating(trading, result, expected):
    from alphapulse.api.risk import package_out
    from alphapulse.models.risk import NewsSummary
    session = AsyncMock()
    session.scalar.return_value = 1
    session.get.return_value = None
    package = NewsSummary(id=1, title='明天开会', result=result, replacement_ids=[])
    assert asyncio.run(package_out(package, session))['title'] == '明天开会'


def test_ranking_invalid_identity_stops_after_one_repair(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    from alphapulse.news_risk.service import SubmissionFailure
    complete = AsyncMock(return_value=(briefing.Ranked(reading=[], notes='Notes'), {'output_tokens': 10}))
    monkeypatch.setattr(briefing, 'complete', complete)
    with pytest.raises(SubmissionFailure, match='missing') as error:
        asyncio.run(briefing.generate(SimpleNamespace(max_tokens=100, context_window=10000), '',
            [{'id': 1, 'revision': 2, 'importance_stars': 3}], ranking_only=True))
    assert complete.await_count == 2
    assert len(error.value.usage['failed_batch']['rounds']) == 2


def test_direct_web_read_preserves_public_reader_rejection(trading, monkeypatch):
    from alphapulse.news_risk import research as module
    reader = module.Research()
    fetch = AsyncMock(side_effect=ValueError('不允许访问非公开地址'))
    monkeypatch.setattr(module, 'fetch_public', fetch)
    with pytest.raises(ValueError, match='非公开'):
        asyncio.run(reader.tool('read_web', {'url': 'http://127.0.0.1'}))
    assert not reader.sources
