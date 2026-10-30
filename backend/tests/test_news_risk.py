"""Risk evidence, identity and model boundaries use synthetic inputs only."""
import asyncio
import json
from datetime import UTC, datetime
import pytest


@pytest.fixture
def risk(trading):
    from alphapulse.news_risk import service
    return service


def label(risk, **values):
    return risk.Label(id=1, title='交易所安全事件', summary='媒体报道交易所安全事件', category='security',
        entities=['Exchange'], stars=5, direction='bearish', verification='unverified', confidence='low',
        reasoning='重大资金安全风险', uncertainty='规模尚待核实', evidence='安全事件', event_id=None, **values)


def test_risk_start_is_beijing_midnight(risk):
    assert risk.START == datetime(2026, 10, 25, 16, tzinfo=UTC)


def test_extreme_unverified_event_preserves_uncertainty(risk):
    item = label(risk)
    risk.validate_labels(risk.Labels(items=[item]), {1: {'title': '安全事件'}}, set())
    assert item.stars == 5 and item.verification == 'unverified'


@pytest.mark.parametrize('evidence', ['安全事件', ' 安全事件\n', '“安全事件”', '「安全事件」', '‘安全事件’', '"安全事件"', "'安全事件'"])
def test_evidence_presentation_quotes_preserve_literal_source(risk, evidence):
    item = label(risk).model_copy(update={'evidence': evidence})
    risk.validate_labels(risk.Labels(items=[item]), {1: {'content': '媒体报道安全事件，规模待核实。'}}, set())
    assert item.evidence == '安全事件'


@pytest.mark.parametrize('evidence', ['“资产已被盗”', '安全…事件', 'security incident', '   ', '“”', '标题\n正文'])
def test_evidence_must_be_nonempty_and_from_one_source_field(risk, evidence):
    item = label(risk).model_copy(update={'evidence': evidence})
    with pytest.raises(ValueError, match='模型证据并非输入原文引用'):
        risk.validate_labels(risk.Labels(items=[item]), {1: {'title': '标题', 'summary': '正文', 'content': '安全事件'}}, set())


def test_evidence_retains_quotes_present_in_source(risk):
    item = label(risk).model_copy(update={'evidence': '“安全事件”'})
    risk.validate_labels(risk.Labels(items=[item]), {1: {'content': '媒体称这是“安全事件”。'}}, set())
    assert item.evidence == '“安全事件”'


@pytest.mark.parametrize('valid', [True, False])
def test_enable_validates_evidence_before_saving_settings(risk, monkeypatch, valid):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI
    import httpx
    from alphapulse.api import risk as api

    session = SimpleNamespace(merge=AsyncMock(), commit=AsyncMock())
    provider = SimpleNamespace(model='news-only')
    monkeypatch.setattr(api, 'provider_for', AsyncMock(return_value=provider))

    async def complete(actual_provider, system, payload, schema):
        assert actual_provider is provider
        news = payload['news'][0]
        assert news['content'] and news['summary']
        evidence = '“'+news['summary']+'”' if valid else '虚构的资产损失6%'
        return risk.Labels(items=[label(risk).model_copy(update={'evidence': evidence})]), {}

    monkeypatch.setattr(api, 'complete', complete)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: None
    app.dependency_overrides[api.get_session] = lambda: session

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.post('/api/risk/enable', json={'provider_id': 1, 'model': 'news-only'})
            assert response.status_code == (200 if valid else 502), response.text
            if valid:
                saved = session.merge.call_args.args[0]
                assert saved.enabled and saved.model == 'news-only'
                session.commit.assert_awaited_once()
            else:
                assert '模型证据并非输入原文引用' in response.json()['detail']
                session.merge.assert_not_awaited()
                session.commit.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'unknown', 'evidence', 'candidate', 'insufficient'])
def test_invalid_results_are_rejected(risk, kind):
    item = label(risk)
    if kind == 'missing': items = []
    elif kind == 'duplicate': items = [item, item]
    else:
        if kind == 'unknown': item.id = 2
        if kind == 'evidence': item.evidence = '捏造的跌幅6%'
        if kind == 'candidate': item.event_id = 999
        if kind == 'insufficient': item.verification = 'insufficient'
        items = [item]
    with pytest.raises(ValueError):
        risk.validate_labels(risk.Labels(items=items), {1: {'title': '安全事件'}}, set())


def test_same_batch_grouping_must_not_cycle(risk):
    first = label(risk)
    second = label(risk).model_copy(update={'id': 2, 'same_event_as': 1})
    inputs = {1: {'title': '安全事件'}, 2: {'title': '安全事件'}}
    risk.validate_labels(risk.Labels(items=[second, first]), inputs, set())
    first.same_event_as = 2
    with pytest.raises(ValueError):
        risk.validate_labels(risk.Labels(items=[first, second]), inputs, set())


def test_truncated_completion_is_not_a_safe_label(risk, monkeypatch):
    from types import SimpleNamespace
    async def stream(*args):
        yield risk.llm.Completion(truncated=True)
    monkeypatch.setattr(risk.llm, 'stream', stream)
    with pytest.raises(ValueError, match='截断'):
        asyncio.run(risk.complete(SimpleNamespace(max_tokens=100, context_window=10000, reasoning_effort=None), '', {}, risk.Labels))


def test_model_override_never_changes_shared_provider(risk):
    from types import SimpleNamespace
    provider = SimpleNamespace(model='original')
    class Session:
        async def get(self, *args): return provider
        def expunge(self, obj): self.detached = obj.model
    session = Session()
    result = asyncio.run(risk.provider_for(session, SimpleNamespace(provider_id=1, model='LUNA')))
    assert session.detached == 'original' and result.model == 'LUNA'


def test_worker_correction_and_late_backfill_keep_current_and_peak_ratings(risk, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from alphapulse.news_risk import worker
    from alphapulse.models import NewsItem
    from alphapulse.models.risk import RiskAnalysis, RiskEvent, RiskSettings

    now = datetime.now(UTC)
    original = label(risk)
    event = RiskEvent(id=7, title=original.title, category='security', entities=['Exchange'], stars=5,
        peak_stars=5, result=original.model_dump(), published_at=now-timedelta(minutes=2), updated_at=now-timedelta(minutes=2))
    rows = [RiskAnalysis(id=i, news_id=i, content_hash=str(i), state='pending', input={
        'title': '安全事件', 'published_at': (now-timedelta(minutes=minutes)).isoformat(),
        'progress_at': (now-timedelta(minutes=minutes)).isoformat()}) for i, minutes in [(2, 1), (1, 3)]]
    correction = original.model_copy(update={'id': 2, 'event_id': 7, 'stars': 1, 'verification': 'refuted'})
    historical = original.model_copy(update={'id': 1, 'event_id': 7})

    class Session:
        def __init__(self): self.selections = iter([rows, [event]])
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, model, identifier, **kwargs):
            if model is RiskSettings: return SimpleNamespace(enabled=True)
            if model is NewsItem: return SimpleNamespace(content_hash=str(identifier))
            if model is RiskEvent: return event
            raise AssertionError(model)
        async def scalars(self, query):
            values = next(self.selections)
            return SimpleNamespace(all=lambda: values)
        async def commit(self): pass

    monkeypatch.setattr(worker, 'SessionLocal', Session)
    monkeypatch.setattr(worker, 'provider_for', AsyncMock(return_value=SimpleNamespace(model='news-only', max_tokens=1000, context_window=200000)))
    monkeypatch.setattr(worker, 'find_candidates', AsyncMock(return_value=([event], {1: 7, 2: 7})))
    monkeypatch.setattr(worker, 'enqueue', AsyncMock())
    monkeypatch.setattr(worker, 'complete', AsyncMock(return_value=(risk.Labels(items=[historical, correction]), {'input_tokens': 10})))
    asyncio.run(worker.analyze())
    assert event.stars == 1 and event.peak_stars == 5
    assert event.result['verification'] == 'refuted'
    assert event.published_at == now-timedelta(minutes=3)
    assert all(row.state == 'done' and row.event_id == 7 for row in rows)
    assert rows[0].usage == {'input_tokens': 10} and rows[1].usage is None


def test_batch_packing_keeps_other_articles_queued_and_retains_linked_event(risk):
    from types import SimpleNamespace
    from alphapulse.news_risk.worker import pack_batch, batch_payload
    now = datetime.now(UTC)
    event = SimpleNamespace(id=7, title='安全事件', category='security', entities=['Exchange'],
        published_at=now, updated_at=now, result={'summary': '安全事件'})
    rows = [SimpleNamespace(id=i, news_id=i, input={'title': '安全事件', 'content': 'x'*1000}) for i in range(10)]
    base = len((risk.SYSTEM + json.dumps(risk.Labels.model_json_schema(), ensure_ascii=False)).encode())
    provider = SimpleNamespace(max_tokens=256, context_window=base+2000)
    packed, candidates, payload = pack_batch(provider, rows, [event], {0: 7})
    assert 0 < len(packed) < len(rows)
    assert candidates == [event] and risk.fits_context(provider, risk.SYSTEM, payload, risk.Labels)
    assert all(len(r.input['content']) == 1000 for r in rows)
    rows[0].input['content'] = 'x'*10000
    packed, candidates, payload = pack_batch(provider, rows, [event], {0: 7})
    assert packed == [rows[0]] and candidates == [event]
    assert not risk.fits_context(provider, risk.SYSTEM, payload, risk.Labels)
    assert len(batch_payload(rows[1:], [])['news']) == 9


def test_context_budget_includes_unicode_and_tool_schema(risk):
    from types import SimpleNamespace
    provider = SimpleNamespace(max_tokens=256, context_window=4096)
    assert not risk.fits_context(provider, risk.SYSTEM, {'content': '风'*1000}, risk.Labels)


@pytest.mark.parametrize('endpoint', ['overview', 'events'])
@pytest.mark.parametrize('hours,status', [(None, 200), ('24', 200), ('48', 200), ('168', 200), ('25', 422), ('0', 422), ('abc', 422)])
def test_window_query_parameters_over_http(risk, monkeypatch, endpoint, hours, status):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI
    import httpx
    from alphapulse.api import risk as api

    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        scalar=AsyncMock(return_value=None), scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: None
    app.dependency_overrides[api.get_session] = lambda: session
    monkeypatch.setattr(api, 'coverage', AsyncMock(return_value={}))
    monkeypatch.setattr(api.redis, 'get', AsyncMock(return_value=None))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get('/api/risk/'+endpoint, params={} if hours is None else {'hours': hours})
            assert response.status_code == status, response.text
            if status == 200:
                assert response.json()['code'] == 0
    asyncio.run(check())
