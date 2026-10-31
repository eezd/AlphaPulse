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


def test_read_web_requires_discovered_url_and_budget(trading):
    from alphapulse.news_risk.research import Research
    research = Research()
    with pytest.raises(ValueError, match='本轮搜索'):
        asyncio.run(research.tool('read_web', {'url': 'https://example.invalid'}))
    research.counts['read_web'] = 4
    with pytest.raises(ValueError, match='预算'):
        asyncio.run(research.tool('read_web', {'url': 'https://example.invalid'}))


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
    from alphapulse.news_risk import worker
    now = datetime.now(UTC)
    event = SimpleNamespace(id=1, published_revision=2, importance_stars=5 if mode.startswith('urgent') else 3,
        category='politics', tags=['政策'], result={'summary': '当前说明'}, analyzed_at=now)
    semantic = [{'id': event.id, 'importance_stars': event.importance_stars, 'result': event.result}]
    fingerprint = worker.briefing.fingerprint(semantic, {}) if mode == 'unchanged' else 'previous'
    latest = SimpleNamespace(created_at=now-timedelta(seconds=30 if mode == 'urgent_wait' else 120), quotes={'fingerprint': fingerprint})
    if mode in ('ordinary_due', 'expired'): latest.quotes['dirty_since'] = (now-timedelta(minutes=16)).isoformat()
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.return_value = SimpleNamespace(enabled=True)
    session.scalar.side_effect = [latest, None]
    session.scalars.return_value = SimpleNamespace(all=lambda: [] if mode == 'expired' else [event])
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
        asyncio.run(briefing.generate(SimpleNamespace(max_tokens=256, context_window=200000), 'rules', [{'id': 1}, {'id': 2}]))
    assert complete.await_count == 1


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
    session.get.return_value = SimpleNamespace(enabled=True); session.scalar.side_effect = [latest, None]
    session.scalars.return_value = SimpleNamespace(all=lambda: [SimpleNamespace(id=1, published_revision=2,
        importance_stars=5, category='politics', tags=[], result=updated[0]['result'], analyzed_at=now)])
    session.add = lambda value: None
    generated = AsyncMock(return_value={'reading': [], 'synthesis': None, 'usage': []})
    monkeypatch.setattr(worker, 'SessionLocal', lambda: session)
    monkeypatch.setattr(worker, 'configuration', AsyncMock(return_value=(SimpleNamespace(model='advanced'), 'rules', {}, 'key')))
    monkeypatch.setattr(briefing, 'generate', generated)
    asyncio.run(worker.outlook())
    assert bool(generated.await_count) is expected
