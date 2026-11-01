"""Risk evidence, identity and model boundaries use synthetic inputs only."""
import asyncio
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


def mock_enable(risk, monkeypatch, valid=True, verification='reported', direction='neutral'):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from alphapulse.news_risk import pipeline, briefing
    from alphapulse.api import risk as api
    provider = SimpleNamespace(model='news-only', max_tokens=1000, context_window=200000)
    session = SimpleNamespace(get=AsyncMock(return_value=None), merge=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(pipeline, 'configuration', AsyncMock(return_value=(provider, 'rules', {}, 'fingerprint')))
    async def complete(provider, system, payload, schema):
        if schema is pipeline.Grouping:
            return pipeline.Grouping( reason='财经维护通知', packages=[pipeline.Match(package_id=None,
                group_key='maintenance', title='维护', category='company', entities=['示例公司'], fact='维护',
                evidence=payload['article']['summary'] if valid else '捏造损失', urgent=False, tags=['维护'], content_kind='statement')]), {}
        if schema is risk.Outlook:
            return risk.Outlook(windows=[risk.Scenario(hours=h, direction='unknown', summary='不足', conditions='不足',
                counter_factors='不足', uncertainty='不足', event_ids=[]) for h in (24, 48, 168)]), {}
        article = payload['articles'][0]
        result = pipeline.EventResult(**{**label(risk).model_dump(exclude={'event_id', 'same_event_as', 'evidence', 'stars'}), 'verification': verification,
            'direction': direction}, timeline_points=[dict(occurred_at=None, time_precision='unknown', time_text='Unknown', reported_at=None, description='Synthetic', verification='insufficient', evidence_refs=[{'article_id':1,'quote':article['summary']}])], facts='维护', timeline='维护',
            asset_impacts='未知', risk_stars=0, importance_stars=3, opinions='待观察', counter_evidence='不足', watchpoints='恢复', claims=[pipeline.Claim(statement='维护', verdict='insufficient', evidence_refs=[])], related_packages=[], excluded_article_ids=[],
            evidence_refs=[pipeline.Evidence(article_id=1, quote=article['summary'])])
        return result, {}
    async def high(provider, system, payload, schema):
        if schema is briefing.Ranked:
            return briefing.Ranked(reading=[briefing.Reading(id=p['id'], revision=p['revision'], priority=50, reason='重要') for p in payload['packages']], notes='虚构测试'), {}
        trend = briefing.Trend(summary='未知', conditions='不足', counter_evidence='不足', watchpoints='后续')
        return briefing.Synthesis(summary='不足', market='未知', short_term=trend, medium_term=trend, references=[]), {}
    monkeypatch.setattr(briefing, 'complete', high)
    monkeypatch.setattr(api, 'complete', complete)
    monkeypatch.setattr(pipeline, 'complete', complete)
    return session


def enable_response(session):
    from fastapi import FastAPI
    import httpx
    from alphapulse.api import risk as api
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: None
    app.dependency_overrides[api.get_session] = lambda: session
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            return await client.post('/api/risk/enable', json={'provider_id': 1, 'model': 'news-only'})
    return asyncio.run(check())


@pytest.mark.parametrize('valid', [True, False])
def test_enable_validates_evidence_before_saving_settings(risk, monkeypatch, valid):
    session = mock_enable(risk, monkeypatch, valid)
    response = enable_response(session)
    assert response.status_code == (200 if valid else 502), response.text
    if valid:
        saved = session.merge.call_args.args[0]
        assert saved.enabled and saved.start_at is not None
        session.commit.assert_awaited_once()
    else:
        assert '原文引用' in response.text
        session.merge.assert_not_awaited()


@pytest.mark.parametrize('verification,direction,status', [
    ('insufficient', 'unknown', 200), ('insufficient', 'neutral', 502), ('insufficient', 'bullish', 502),
    ('insufficient', 'bearish', 502), ('insufficient', 'mixed', 502), ('reported', 'neutral', 200),
])
def test_enable_requires_consistent_uncertainty_without_changing_model_output(risk, monkeypatch, verification, direction, status):
    session = mock_enable(risk, monkeypatch, verification=verification, direction=direction)
    response = enable_response(session)
    assert response.status_code == status, response.text
    if status == 200:
        session.merge.assert_awaited_once()
    else:
        assert '无法判断方向' in response.text
        session.merge.assert_not_awaited()


def test_uncertainty_contract_reaches_the_model_with_tool_schema(risk, monkeypatch):
    from types import SimpleNamespace
    item = label(risk).model_copy(update={'verification': 'insufficient', 'direction': 'unknown'})

    async def stream(provider, system, messages, tools, effort):
        assert 'verification="insufficient", direction="unknown"' in system
        fields = tools[0].parameters['$defs']['Label']['properties']
        assert 'insufficient' in fields['direction']['description']
        assert 'unknown' in fields['verification']['description']
        yield risk.llm.Completion(tool_calls=[{'name': 'submit', 'arguments': {'items': [item.model_dump()]}}])

    monkeypatch.setattr(risk.llm, 'stream', stream)
    payload = {'news': [{'id': 1, 'title': '安全事件'}], 'candidates': []}
    provider = SimpleNamespace(max_tokens=256, context_window=200000, reasoning_effort=None)
    result, _ = asyncio.run(risk.complete(provider, risk.SYSTEM, payload, risk.Labels))
    risk.validate_labels(result, {1: payload['news'][0]}, set())
    assert result.items[0].direction == 'unknown'


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
        yield risk.llm.Completion(truncated=True, usage={'output_tokens': 100})
    monkeypatch.setattr(risk.llm, 'stream', stream)
    with pytest.raises(risk.OutputTruncated, match='截断') as exc:
        asyncio.run(risk.complete(SimpleNamespace(max_tokens=100, context_window=10000, reasoning_effort=None), '', {}, risk.Labels))
    assert exc.value.usage == {'output_tokens': 100}


def test_model_override_never_changes_shared_provider(risk):
    from types import SimpleNamespace
    provider = SimpleNamespace(model='original')
    class Session:
        async def get(self, *args): return provider
        def expunge(self, obj): self.detached = obj.model
    session = Session()
    result = asyncio.run(risk.provider_for(session, SimpleNamespace(provider_id=1, model='LUNA')))
    assert session.detached == 'original' and result.model == 'LUNA'


def test_debounce_is_bounded_and_urgent_bypasses_wait(risk):
    from datetime import timedelta
    from types import SimpleNamespace
    from alphapulse.news_risk.worker import schedule
    now = datetime.now(UTC)
    event = SimpleNamespace(revision=1, result={'summary': '原总结'}, dirty_since=now-timedelta(seconds=170))
    schedule(event, now, False)
    assert event.due_at == now+timedelta(seconds=10)
    assert event.revision == 2
    schedule(event, now, True)
    assert event.due_at == now


def test_long_sources_are_split_without_losing_text(risk, monkeypatch):
    from types import SimpleNamespace
    from alphapulse.news_risk import pipeline
    provider = SimpleNamespace(max_tokens=100, context_window=10000)
    article = {'id': 1, 'title': '安全事件', 'summary': '', 'content': '安'*16000}
    seen = []
    async def complete(provider, system, payload, schema):
        if payload['task'] == 'extract':
            seen.append(payload['content'])
            return schema(facts='事实', evidence_refs=[pipeline.Evidence(article_id=1, quote=payload['content'][:1])]), {}
        return pipeline.EventResult(**label(risk).model_dump(exclude={'event_id', 'same_event_as', 'evidence', 'stars'}), timeline_points=[dict(occurred_at=None, time_precision='unknown', time_text='Unknown', reported_at=None, description='Synthetic', verification='insufficient', evidence_refs=[{'article_id':1,'quote':'安全事件'}])], facts='事实', timeline='时间', asset_impacts='资产',
            risk_stars=5, importance_stars=5, opinions='待观察', counter_evidence='不足', watchpoints='恢复', claims=[pipeline.Claim(statement='事件', verdict='insufficient', evidence_refs=[])], related_packages=[], excluded_article_ids=[], evidence_refs=[pipeline.Evidence(article_id=1, quote='安全事件')]), {}
    monkeypatch.setattr(pipeline, 'complete', complete)
    result, _ = asyncio.run(pipeline.summarize_articles(provider, 'rules', [article], {'id': 1, 'fact': 'Synthetic event'}))
    assert ''.join(seen) == article['title']+'\n\n'+article['content']
    assert result.risk_stars == 5


def test_context_budget_includes_unicode_and_tool_schema(risk):
    from types import SimpleNamespace
    provider = SimpleNamespace(max_tokens=256, context_window=4096)
    assert not risk.fits_context(provider, risk.SYSTEM, {'content': '风'*1000}, risk.Labels)


@pytest.mark.parametrize('endpoint', ['overview', 'summaries'])
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


def test_fresh_settings_can_be_saved_without_editing_any_fields(risk):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI
    import httpx
    from alphapulse.api import risk as api
    session = SimpleNamespace(get=AsyncMock(return_value=None), scalar=AsyncMock(return_value=None),
        merge=AsyncMock(), commit=AsyncMock())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: None
    app.dependency_overrides[api.get_session] = lambda: session
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get('/api/risk/settings')
            assert response.status_code == 200
            value = response.json()['data']
            assert value['second_model'] == value['third_model'] == ''
            response = await client.put('/api/risk/settings', json=value)
            assert response.status_code == 200, response.text
    asyncio.run(check())
    saved = session.merge.call_args.args[0]
    assert saved.enabled is False and saved.third_model == ''
    session.commit.assert_awaited_once()


@pytest.mark.parametrize('state,published,reason', [('done', 3, '成功结果'), ('pending', 2, '排队'), ('processing', 2, '正在处理')])
def test_old_failed_analysis_cannot_requeue_a_completed_or_running_revision(risk, state, published, reason):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from alphapulse.api import risk as api
    event = SimpleNamespace(revision=3, published_revision=published, state=state)
    row = SimpleNamespace(state='failed', summary_id=1, revision=3)
    session = SimpleNamespace(get=AsyncMock(side_effect=[row, event]), commit=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.retry(7, session, stage='analysis'))
    assert exc.value.status_code == 409 and reason in exc.value.detail
    assert event.state == state
    session.commit.assert_not_awaited()


def test_completed_version_recovers_a_previously_stuck_pending_package(risk, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from alphapulse.news_risk import worker
    event = SimpleNamespace(review_config=None, result=None, id=1, revision=3, published_revision=3, state='pending', error='Old failure',
        dirty_since=datetime.now(UTC), title='Statement', fact='Statement', category='politics', tags=['Statement'], content_kind='statement')
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.return_value = SimpleNamespace(enabled=True)
    session.scalar.side_effect = [event, SimpleNamespace(state='done')]
    session.scalars.return_value = SimpleNamespace(all=lambda: [SimpleNamespace(id=1, input={'title': 'Statement'})])
    monkeypatch.setattr(worker, 'SessionLocal', lambda: session)
    monkeypatch.setattr(worker, 'configuration', AsyncMock(return_value=(SimpleNamespace(model='medium'), 'rules', {}, 'key')))
    analyze = AsyncMock()
    monkeypatch.setattr(worker, 'summarize_articles', analyze)
    asyncio.run(worker.summarize())
    assert event.state == 'done' and event.error is None and event.dirty_since is None
    session.commit.assert_awaited_once()
    analyze.assert_not_awaited()
