"""新报表的成本边界、时间口径与证据契约；外部 I/O 使用合成边界。"""
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def domain(app_env):
    from alphapulse.briefings import contracts, reports, schedule
    return N(contracts=contracts, reports=reports, schedule=schedule)


def report_fixture(kind='morning', count=2):
    return N(id=7, kind=kind, report_date=date(2026, 11, 10),
             window_start=datetime(2026, 11, 10, tzinfo=UTC),
             window_end=datetime(2026, 11, 11, tzinfo=UTC), coverage={'total': count, 'missing': 0},
             market=[], lane='historical_backfill', regenerate=False, request_id=None,
             input={'analysis_ids': list(range(1, count + 1)),
                    'cards': [{'analysis_id': i} for i in range(1, count + 1)]})


def report_result(contracts, ids=()):
    return contracts.ReportResult(title='合成报告', summary='事实与判断分别列示', sections=[],
                                   highlight_analysis_ids=list(ids)[:3])


@pytest.mark.parametrize('kind,hours,due_hours', [('morning', (0, 9), 9), ('evening', (9, 18), 18),
                                                ('intraday', (18, 24), 24), ('daily', (0, 24), 32)])
def test_exact_windows_and_five_minute_delay(domain, kind, hours, due_hours):
    origin = datetime(2026, 11, 10, tzinfo=domain.schedule.SHANGHAI)
    start, end, due = domain.schedule.window(kind, origin.date())
    assert start == origin + timedelta(hours=hours[0])
    assert end == origin + timedelta(hours=hours[1])
    assert due == origin + timedelta(hours=due_hours, minutes=5)
    assert start.tzinfo == UTC
    if kind != 'daily':
        assert end <= due
    assert domain.schedule.window('morning', origin.date())[1] == domain.schedule.window('evening', origin.date())[0]
    assert domain.schedule.window('intraday', origin.date())[1] == domain.schedule.window('morning', origin.date() + timedelta(days=1))[0]


@pytest.mark.parametrize('selected,expected', [
    ({}, date.min),
    ({'backfill_start': None}, date.min),
    ({'backfill_start': '2026-11-10'}, date(2026, 11, 10)),
    ({'period_backfill_start': '2026-11-10', 'daily_backfill_start': '2026-11-01'}, date(2026, 11, 1)),
    ({'period_backfill_start': '2026-11-01', 'daily_backfill_start': '2026-11-10'}, date(2026, 11, 1)),
    ({'period_backfill_start': '2026-11-10', 'daily_backfill_start': None}, date.min),
    ({'backfill_start': None, 'period_backfill_start': '2026-11-10'}, date.min),
    ({'backfill_start': '2026-11-10', 'daily_backfill_start': '2026-11-01'}, date(2026, 11, 10)),
])
def test_global_backfill_range_and_legacy_config_reading(domain, selected, expected):
    assert domain.schedule.backfill_start(selected) == expected
    for kind in ('morning', 'evening', 'intraday', 'daily'):
        assert domain.schedule.includes_report(selected, kind, expected)
        if expected != date.min:
            assert not domain.schedule.includes_report(selected, kind, expected - timedelta(days=1))
    if expected != date.min:
        assert domain.schedule.backfill_cutoff(selected) == datetime.combine(expected, datetime.min.time(),
                                                                             domain.schedule.SHANGHAI)
    else:
        assert domain.schedule.backfill_cutoff(selected) == datetime.min.replace(tzinfo=UTC)


@pytest.mark.parametrize('selected,expected', [
    ({'period_backfill_start': '2026-11-01', 'daily_backfill_start': '2026-11-10'}, '2026-11-01'),
    ({'period_backfill_start': '2026-11-10', 'daily_backfill_start': None}, None),
])
def test_legacy_backfill_normalizes_once_and_preserves_model_configuration(app_env, selected, expected):
    from alphapulse.briefings.config import normalize_backfill
    cursor = date(2026, 11, 15)
    row = N(config={**selected, 'analysis': {'model': 'unchanged'}, 'concurrency': 5},
            history_start=date(2026, 11, 1), history_end=cursor, history_cursor=cursor, history_enrolled=True)
    normalize_backfill(row)
    assert row.config == {'analysis': {'model': 'unchanged'}, 'concurrency': 5, 'backfill_start': expected}
    assert row.history_start is row.history_end is row.history_cursor is None
    assert row.history_enrolled is False
    row.history_cursor = cursor
    normalize_backfill(row)
    assert row.history_cursor == cursor


def test_backfill_date_serializes_to_json_and_rejects_future_days(app_env):
    from alphapulse.briefings.config import SettingsInput
    from alphapulse.briefings.schedule import SHANGHAI
    model = {'provider_id': 1, 'model': 'synthetic', 'skill_id': 1, 'max_tokens': 4096}
    fields = {stage: model for stage in ('analysis', 'period', 'daily')}
    today = datetime.now(SHANGHAI).date()
    value = SettingsInput(**fields, base_revision=1, backfill_start=today)
    assert value.model_dump(mode='json')['backfill_start'] == today.isoformat()
    assert SettingsInput(**fields, base_revision=1).backfill_start is None
    with pytest.raises(ValueError, match='回补起始日期不能晚于北京时间今天'):
        SettingsInput(**fields, base_revision=1, backfill_start=today + timedelta(days=1))
    for field in ('period_backfill_start', 'daily_backfill_start'):
        with pytest.raises(ValueError, match='Extra inputs are not permitted'):
            SettingsInput(**fields, base_revision=1, **{field: today})


@pytest.mark.parametrize('day,hour', [(date(2026, 1, 10), 22), (date(2026, 7, 10), 21)])
def test_us_regular_open_observes_dst_without_assuming_trading_day(domain, day, hour):
    value = datetime.fromisoformat(domain.schedule.regular_us_open(day))
    assert (value.hour, value.minute) == (hour, 30)


@pytest.mark.parametrize('extra,basis', [({}, 'published_at'), ({'published_at_precision': 'date'}, 'first_seen_at'),
    ({'time_precision': 'unknown'}, 'first_seen_at'), ({'published_at_unreliable': True}, 'first_seen_at')])
def test_news_time_precision(domain, extra, basis):
    news = N(extra=extra, published_at=datetime(2026, 11, 9, tzinfo=UTC), first_seen_at=datetime(2026, 11, 10, tzinfo=UTC))
    assert domain.schedule.effective_time(news) == (getattr(news, basis), basis)


@pytest.mark.parametrize('total,missing,state', [(0, 0, 'ready'), (20, 1, 'ready'), (19, 1, 'waiting_coverage'),
    (200, 10, 'ready'), (49, 10, 'blocked'), (50, 10, 'blocked'), (51, 10, 'waiting_coverage'),
    (5, 1, 'waiting_coverage'), (10, 9, 'waiting_coverage')])
def test_coverage_exact_boundaries_and_small_sample(domain, total, missing, state):
    now = datetime(2026, 11, 10, 1, 5, tzinfo=UTC)
    result, coverage, next_check = domain.schedule.coverage_decision(total, missing, now, None)
    assert result == state and coverage['available'] == total - missing
    assert (next_check == now + timedelta(minutes=15)) if state == 'waiting_coverage' else next_check is None


def test_coverage_deadline_and_upstream_do_not_block_empty_or_allowed_gaps(domain):
    now = datetime(2026, 11, 10, 1, 5, tzinfo=UTC)
    for minute in (0, 15, 30, 45):
        state, _, check = domain.schedule.coverage_decision(10, 1, now + timedelta(minutes=minute), now)
        assert state == 'waiting_coverage' and check == now + timedelta(minutes=minute + 15)
    assert domain.schedule.coverage_decision(10, 1, now + timedelta(hours=1), now)[0] == 'blocked'
    assert domain.schedule.coverage_decision(10, 1, now, now, upstream=True)[0] == 'blocked'
    for total, missing in ((0, 0), (20, 1)):
        assert domain.schedule.coverage_decision(total, missing, now, now, upstream=True)[0] == 'ready'


@pytest.mark.parametrize('score,stars', [(0, 1), (19, 1), (20, 2), (39, 2), (40, 3), (69, 3), (70, 4), (89, 4), (90, 5), (100, 5)])
def test_stars_are_derived_once(domain, score, stars):
    assert domain.contracts.stars(score) == stars
    with pytest.raises(ValueError):
        domain.contracts.stars(101)


@pytest.fixture
def analysis_result(domain):
    return domain.contracts.NewsAnalysisResult(
        keywords=[{'canonical': '以太坊', 'aliases': ['ETH'], 'kind': 'asset', 'quote': 'ETH'}],
        summary='摘要', facts=[{'text': 'ETH 上涨', 'quote': '上涨5%'}], impact='报道的影响判断', importance_score=72,
        importance_reason='行业影响', primary_region={'code': 'CRYPTO', 'name': '加密货币'}, related_regions=[],
        assets=[], uncertainties=['尚待确认'], scenarios=[])


def test_frozen_evidence_and_references_reject_fabrications(domain, analysis_result):
    c, value = domain.contracts, analysis_result
    c.validate_evidence(value, {'title': 'ETH 上涨5%'})
    with pytest.raises(ValueError, match='事实证据'):
        c.validate_evidence(value, {'title': 'ETH 下跌'})
    with pytest.raises(ValueError, match='关键词证据'):
        c.validate_evidence(value, {'title': '上涨5%'})
    with pytest.raises(ValueError, match='未实际读取'):
        c.validate_references(report_result(c, [9]), {1})
    with pytest.raises(ValueError, match='不能重复'):
        c.validate_references(report_result(c, [1, 1]), {1})


@pytest.mark.parametrize('source,canonical,aliases', [
    ('NVIDIA publishes results', '英伟达', ['NVIDIA']),
    ('Ethereum (ETH)上涨', '以太坊', ['Ethereum', 'ETH']),
    ('风险\n仍在', '持续风险', ['风险\n仍在']),
])
def test_canonical_can_be_normalized_but_aliases_and_quotes_stay_original(domain, analysis_result, source, canonical, aliases):
    c = domain.contracts
    value = analysis_result.model_copy(update={
        'keywords': [c.Keyword(canonical=canonical, aliases=aliases, kind='topic', quote=source)],
        'facts': [c.Fact(text='来源所述内容', quote=source)]})
    c.validate_evidence(value, {'content_text': source})
    assert value.keywords[0].canonical == canonical and value.keywords[0].aliases == aliases


@pytest.mark.parametrize('alias', ['以太坊', 'NVDA', 'eth', '风险仍在', 'NVIDIA\n上涨5%'])
def test_aliases_reject_translations_invented_codes_case_changes_and_stitched_fields(domain, analysis_result, alias):
    c = domain.contracts
    value = analysis_result.model_copy(update={
        'keywords': [c.Keyword(canonical='以太坊', aliases=[alias], kind='asset', quote='ETH')]})
    with pytest.raises(ValueError, match='关键词别名'):
        c.validate_evidence(value, {'title': 'ETH NVIDIA', 'summary': '上涨5%', 'content_text': '风险\n仍在'})


@pytest.mark.parametrize('target', ['fact', 'keyword', 'review'])
@pytest.mark.parametrize('quote', ['ETH\n上涨5%', ' ', '\n'])
def test_evidence_rejects_cross_field_and_whitespace_quotes(domain, analysis_result, target, quote):
    c = domain.contracts
    if target == 'review':
        value = c.HighlightReview(analysis_id=1, facts=[c.Fact(text='来源主张', quote=quote)], impact='判断', uncertainties=[])
    elif target == 'fact':
        value = analysis_result.model_copy(update={'facts': [c.Fact(text='来源主张', quote=quote)]})
    else:
        value = analysis_result.model_copy(update={
            'keywords': [c.Keyword(canonical='以太坊', aliases=['ETH'], kind='asset', quote=quote)]})
    with pytest.raises(ValueError, match='证据'):
        c.validate_evidence(value, {'title': 'ETH', 'summary': '上涨5%', 'content_text': '风险\n仍在 '})


def test_keyword_quote_cannot_omit_middle_words(domain, analysis_result):
    c = domain.contracts
    article = {'content_text': 'They recovered more than 200 units of ABC.'}
    keyword = c.Keyword(canonical='ABC', aliases=['ABC'], kind='asset', quote='recovered ABC')
    value = analysis_result.model_copy(update={'keywords': [keyword], 'facts': [c.Fact(text='ABC', quote='ABC')]})
    with pytest.raises(ValueError, match='关键词证据'):
        c.validate_evidence(value, article)
    value.keywords[0].quote = 'ABC'
    c.validate_evidence(value, article)
    assert '不能删掉中间的数字、限定词后把两端重新接起来' in c.ANALYSIS_SKILL


@pytest.mark.parametrize('count,final_fits,expected_calls', [(16, True, 17), (17, True, 0), (16, False, 16)])
def test_period_plan_is_bounded_and_never_recursively_compacts(domain, monkeypatch, count, final_fits, expected_calls):
    r = domain.reports
    report = report_fixture(count=count)
    async def part(*args):
        payload = args[4]
        ids = [item['analysis_id'] for item in payload.get('cards', [])]
        return report_result(domain.contracts, ids)
    complete = AsyncMock(side_effect=part)
    monkeypatch.setattr(r, 'run_part', complete)
    def fits(provider, system, payload, schema):
        op = payload['operation']
        return len(payload['cards']) <= 1 if op == 'period_map' else final_fits if op == 'period_reduce' else False
    monkeypatch.setattr(r.model, 'fits_context', fits)
    if count > 16 or not final_fits:
        with pytest.raises(r.BudgetBlocked):
            asyncio.run(r.generate(report, 1, N(), '', {}))
    else:
        asyncio.run(r.generate(report, 1, N(), '', {}))
    assert complete.await_count == expected_calls
    assert len([call for call in complete.await_args_list if call.args[3] == 'period_final']) == (1 if count <= 16 and final_fits else 0)


def test_daily_reads_only_nine_frozen_highlights_and_one_final(domain, monkeypatch):
    r, c = domain.reports, domain.contracts
    report = report_fixture('daily', 30)
    report.input = {'operation': 'daily', 'analysis_ids': list(range(1, 31)),
        'dependencies': [{'report_id': i, 'output': {}} for i in (1, 2, 3)],
        'highlights': [{'analysis_id': i, 'article': {'title': '冻结原文'}} for i in range(1, 10)]}
    calls = []
    async def part(*args):
        key, stage, payload, schema, validator = args[2], args[3], args[4], args[8], args[10]
        calls.append((key, stage, payload))
        output = (c.HighlightReview(analysis_id=payload['analysis_id'], facts=[{'text': '事实', 'quote': '冻结原文'}], impact='判断', uncertainties=[])
                  if schema is c.HighlightReview else report_result(c, [1]))
        validator(output)
        return output
    monkeypatch.setattr(r, 'run_part', part)
    monkeypatch.setattr(r.model, 'fits_context', lambda provider, system, payload, schema: 'highlights' not in payload)
    asyncio.run(r.generate(report, 1, N(), '', {}))
    assert len(calls) == 10 and [item[1] for item in calls].count('daily_final') == 1
    assert all(item[2]['article']['title'] == '冻结原文' for item in calls[:-1])
    assert 'highlights' not in calls[-1][2] and len(calls[-1][2]['reviews']) == 9


def test_empty_period_and_daily_make_zero_model_steps(domain, monkeypatch):
    complete = AsyncMock()
    monkeypatch.setattr(domain.reports, 'run_part', complete)
    for kind in ('morning', 'daily'):
        output = asyncio.run(domain.reports.generate(report_fixture(kind, 0), 1, N(), '', {}))
        assert not output.sections and '未调用模型' in output.summary
    complete.assert_not_awaited()


@pytest.mark.parametrize('historical,pending,manual,state', [
    (True, 1, False, 'waiting_backfill'), (True, None, False, 'blocked'), (False, 1, False, 'blocked'),
    (False, 1, True, 'waiting_backfill'), (False, None, True, 'blocked'),
])
def test_daily_missing_dependency_never_rereads_the_day(domain, historical, pending, manual, state):
    now = datetime(2026, 11, 11, 2, 0, tzinfo=UTC)
    report = report_fixture('daily')
    report.lane = 'historical_backfill' if historical else 'live'
    report.request_id = 'manual' if manual else None
    report.first_check_at = now - timedelta(hours=1)
    report.due_at = report.first_check_at
    session = N(scalar=AsyncMock(side_effect=[None, pending]), scalars=AsyncMock())
    assert asyncio.run(domain.reports.prepare_daily(session, report, now)) is False
    assert report.state == state
    session.scalars.assert_not_awaited()


def test_upstream_requires_active_requests_all_waiting_and_ten_minutes(app_env, monkeypatch):
    from alphapulse.ai import progress
    records = {'a': {'configuration': '1:model', '_request_active': True, '_request_started': 100, 'wait_id': 'a'},
               'b': {'configuration': '1:model', '_request_active': True, '_request_started': 200, 'wait_id': 'b'}}
    monkeypatch.setattr(progress, 'active', records)
    monkeypatch.setattr(progress, 'last_success', {})
    assert not progress.upstream_failures(699)
    assert set(progress.upstream_failures(700)) == {'1:model'}
    records['b']['wait_id'] = None
    assert not progress.upstream_failures(900)
    records['b']['wait_id'] = 'b'
    progress.last_success['1:model'] = 400
    assert not progress.upstream_failures(999)
    assert progress.upstream_failures(1000)
    records.clear()
    assert not progress.upstream_failures(5000)


@pytest.mark.parametrize('count,aligned,status', [(2, 0, 'complete'), (1, 0, 'insufficient_data'), (2, 1, 'insufficient_data')])
def test_market_exact_window_with_no_ticker_or_neighbor_fallback(app_env, count, aligned, status):
    from alphapulse.briefings import market
    start, end = datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 10, 0, 2, tzinfo=UTC)
    left = int(start.timestamp() * 1000)
    session = N(scalars=AsyncMock(return_value=[N(market='spot', symbol='BTCUSDT'), N(market='um', symbol='BTCUSDT')]),
        execute=AsyncMock(return_value=N(one=lambda: (count, left, left + 60000))),
        scalar=AsyncMock(side_effect=[aligned, Decimal('100'), Decimal('110')] * 2 if status == 'complete' else [aligned] * 2))
    items = asyncio.run(market.snapshot(session, start, end))
    assert all(item['status'] == status for item in items)
    assert items[0]['market_type'] == '现货' and items[1]['market_type'] == '永续合约'
    assert all(item['return_pct'] == ('10.0' if status == 'complete' else None) for item in items)
