import asyncio
import copy
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse, Response
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from alphapulse.core.us_news import SOURCES, article, official_url, published, selection


def spider(trading, source='ofac'):
    from alphapulse.crawler.spiders.base import RunState
    from alphapulse.crawler.spiders.us_official import USOfficialSpider
    obj = USOfficialSpider(name=source)
    obj.name = source
    now = datetime(2026, 11, 4, tzinfo=UTC)
    obj.run = RunState(uuid.uuid4(), 'schedule', now, now - timedelta(hours=72))
    obj.run.history_target = datetime(2025, 1, 1, tzinfo=UTC)
    obj.run.history_deadline = time.monotonic() + 20
    obj.cursor = {'history': {'entry': 0, 'pending': []}, 'realtime': {'entry': 0, 'pending': []}}
    obj.lanes = {}
    return obj


def response(url, body, **meta):
    return HtmlResponse(url, body=body.encode(), encoding='utf-8', request=Request(url, meta={'raw_response_id': 12, **meta}))


def collection_sample(source, suffix=''):
    import gzip
    from pathlib import Path
    samples = json.load(gzip.open(Path(__file__).parent / 'fixtures/us_news/collection_failures.json.gz', 'rt'))
    return next(s for s in samples if s['source'] == source and s['url'].endswith(suffix))


def test_real_empty_register_day_continues_to_previous_day(trading):
    obj = spider(trading, 'federal_register')
    obj.cursor['realtime']['day'] = '2026-11-05'
    sample = collection_sample('federal_register')
    request, = obj.parse_listing(response(sample['url'], sample['body'], lane='realtime'))
    assert '2026-11-04' in request.url
    assert not obj.run.errors


@pytest.mark.parametrize('recent', [False, True])
def test_register_completed_body_checkpoint_matches_discovery_after_restart(trading, recent):
    obj = spider(trading, 'federal_register')
    body = 'https://www.federalregister.gov/documents/full_text/html/2026/11/03/2026-22248.html'
    metadata = 'https://www.federalregister.gov/api/v1/documents/2026-22248.json'
    obj.cursor['checked'] = {body: (obj.run.started_at - timedelta(hours=1 if recent else 25)).timestamp()}
    obj.first_requests()
    listing = json.dumps({'count': 1, 'results': [{'document_number': '2026-22248'}]})
    requests = list(obj.parse_listing(response(SOURCES[obj.name].entries[0], listing, lane='realtime')))
    assert any(r.url == metadata for r in requests) is not recent


def test_register_completes_metadata_only_after_body_and_pdf(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    obj = spider(trading, 'federal_register')
    metadata = 'https://www.federalregister.gov/api/v1/documents/2026-22248.json'
    body = 'https://www.federalregister.gov/documents/full_text/html/2026/11/03/2026-22248.html'
    pdf = 'https://www.govinfo.gov/content/pkg/FR-2026-11-03/pdf/2026-22248.pdf'
    doc = {'url': metadata, 'kind': 'register'}
    obj.cursor['realtime'].update(pending=[doc], entry=len(SOURCES[obj.name].entries))
    data = {'body_html_url': body, 'title': 'Digital asset exchange rule', 'publication_date': '2026-11-03', 'html_url': 'https://www.federalregister.gov/documents/2026/11/03/2026-22248/rule', 'document_number': '2026-22248', 'pdf_url': pdf}
    async def parse(res):
        return [item async for item in obj.parse_document(res)]
    body_request, = asyncio.run(parse(response(metadata, json.dumps(data), lane='realtime', document=doc)))
    assert metadata not in obj.cursor.get('checked', {})
    pdf_request, = asyncio.run(parse(response(body, '<p>This official rule regulates digital asset exchanges and financial institutions.</p>', **body_request.meta)))
    assert metadata not in obj.cursor.get('checked', {})
    monkeypatch.setattr(us_official, 'pdf_text', lambda value: 'The official digital asset exchange rule and supporting material.')
    list(obj.parse_attachment(Response(pdf, body=b'%PDF-test', request=pdf_request.replace(meta={**pdf_request.meta, 'raw_response_id': 13}))))
    assert metadata in obj.cursor['checked'] and body in obj.cursor['checked']


@pytest.mark.parametrize('data', [{'count': 2}, {'count': '0', 'description': 'x'}, {'count': 0}, {'count': 0, 'description': 'x', 'results': None}])
def test_register_does_not_hide_malformed_responses(trading, data):
    obj = spider(trading, 'federal_register')
    with pytest.raises(ValueError, match='列表结构'):
        list(obj.parse_listing(response(SOURCES[obj.name].entries[0], json.dumps(data), lane='realtime')))


@pytest.mark.parametrize('source', ['treasury', 'cftc', 'fincen', 'whitehouse'])
def test_real_realtime_lists_filter_dated_old_documents(trading, source):
    obj = spider(trading, source)
    sample = collection_sample(source, '/press-releases' if source == 'fincen' else '')
    # All sample pages precede January 2027; no old body should be requested.
    obj.run.cutoff = datetime(2027, 1, 1, tzinfo=UTC)
    requests = list(obj.parse_listing(response(sample['url'], sample['body'], lane='realtime')))
    pending = obj.cursor['realtime'].get('pending', [])
    assert not pending
    assert all(not r.meta.get('document') for r in requests)


def test_treasury_real_realtime_stops_at_date_boundary_before_broken_pagination(trading):
    obj = spider(trading, 'treasury')
    sample = collection_sample('treasury')
    obj.cursor['realtime']['page'] = sample['url']
    requests = list(obj.parse_listing(response(sample['url'], sample['body'], lane='realtime')))
    assert requests and requests[0].meta['document']['kind'] == 'article'
    assert obj.cursor['realtime']['next'] is None
    assert all(published(d['listing_date'])[0].date() >= obj.run.cutoff.date() for d in obj.cursor['realtime']['pending'])
    # The same response cannot establish historical coverage through 2025.
    with pytest.raises(ValueError, match='重复当前页面'):
        list(obj.parse_listing(response(sample['url'], sample['body'], lane='history')))


@pytest.mark.parametrize('suffix', ['/news/enforcement-actions', '/resources/advisoriesbulletinsfact-sheets'])
def test_fincen_real_pdf_tables_finish_outside_realtime_window(trading, suffix):
    obj = spider(trading, 'fincen')
    sample = collection_sample('fincen', suffix)
    obj.cursor['realtime']['entry'] = SOURCES['fincen'].entries.index(sample['url'])
    requests = list(obj.parse_listing(response(sample['url'], sample['body'], lane='realtime')))
    assert not obj.cursor['realtime'].get('pending')
    assert all(not r.meta.get('document') for r in requests)
    assert not obj.run.errors


@pytest.mark.parametrize('suffix,title', [('/news/enforcement-actions', 'Bank of America'), ('/resources/advisoriesbulletinsfact-sheets', 'Foreign-Agent-Directed')])
def test_fincen_real_pdf_tables_preserve_title_and_date_for_history(trading, suffix, title):
    obj = spider(trading, 'fincen')
    sample = collection_sample('fincen', suffix)
    request, = obj.parse_listing(response(sample['url'], sample['body'], lane='history'))
    doc = request.meta['document']
    assert doc['kind'] == 'article' and doc['format'] == 'pdf'
    assert title in doc['title'] and len(doc['listing_date']) == 10
    assert doc['listing_url'] == sample['url']
    assert request.url.endswith('.pdf')


def test_fincen_pdf_keeps_listing_date_raw_evidence_and_selection(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    obj = spider(trading, 'fincen')
    doc = {'url': 'https://www.fincen.gov/system/files/crypto.pdf', 'kind': 'article', 'format': 'pdf', 'title': 'Crypto exchange consent order', 'listing_date': '2026-11-03', 'listing_url': SOURCES['fincen'].entries[1]}
    obj.cursor['realtime'].update(pending=[doc], entry=len(SOURCES['fincen'].entries))
    monkeypatch.setattr(us_official, 'pdf_text', lambda body: 'This enforcement action concerns a virtual currency exchange financial institution.')
    request = Request(doc['url'], meta={'lane': 'realtime', 'document': doc, 'raw_response_id': 12})
    async def collect():
        return [item async for item in obj.parse_document(Response(doc['url'], body=b'%PDF-test', request=request))]
    item, = asyncio.run(collect())
    assert item['published_at'] == datetime(2026, 11, 3, tzinfo=UTC)
    assert item['raw_response_id'] == 12
    assert item['extra']['published_at_precision'] == 'date'
    assert item['extra']['published_at_basis'] == 'official_listing'
    assert item['extra']['selection_reasons']
    assert doc['url'] in obj.cursor['checked']


def test_fincen_invalid_pdf_remains_a_document_gap(trading):
    obj = spider(trading, 'fincen')
    doc = {'url': 'https://www.fincen.gov/system/files/crypto.pdf', 'kind': 'article', 'format': 'pdf', 'title': 'Crypto exchange consent order', 'listing_date': '2026-11-03', 'listing_url': SOURCES['fincen'].entries[1]}
    obj.cursor['realtime'].update(pending=[doc], entry=len(SOURCES['fincen'].entries))
    async def collect():
        return [item async for item in obj.parse_document(response(doc['url'], '<h1>Document</h1><p>The server returned an HTML document instead of the expected official PDF.</p>', lane='realtime', document=doc))]
    assert not asyncio.run(collect())
    assert doc['url'] not in obj.cursor.get('checked', {})
    assert obj.cursor['document_retries'][doc['url']]['attempts'] == 1
    assert '非 PDF' in obj.run.errors[0]


def test_fincen_pdf_table_missing_date_fails_explicitly(trading):
    obj = spider(trading, 'fincen')
    body = '<main><h1>Actions</h1><table><tr><td><a href="/system/files/order.pdf">Consent order</a></td><td></td></tr></table></main>'
    with pytest.raises(ValueError, match='发布时间'):
        list(obj.parse_listing(response(SOURCES['fincen'].entries[1], body, lane='realtime')))


def test_fincen_pdf_table_does_not_invent_day_for_current_month(trading):
    obj = spider(trading, 'fincen')
    body = '<main><h1>Actions</h1><table><tr><td><a href="/system/files/order.pdf">Consent order</a></td><td>11/2026</td></tr></table></main>'
    with pytest.raises(ValueError, match='发布时间'):
        list(obj.parse_listing(response(SOURCES['fincen'].entries[1], body, lane='realtime')))


def test_cftc_commissioner_index_is_not_a_speech(trading):
    obj = spider(trading, 'cftc')
    body = '<main><h1>Speeches</h1><a href="/PressRoom/SpeechesTestimony/MichaelSelig/index.htm">Commissioner</a><a href="/PressRoom/SpeechesTestimony/opaselig1">Speech</a></main>'
    request, = obj.parse_listing(response(SOURCES['cftc'].entries[1], body, lane='realtime'))
    assert request.url.endswith('/opaselig1')
    assert len(obj.cursor['realtime']['pending']) == 1


def test_cftc_drops_only_confirmed_profile_gaps_from_old_cursor(trading):
    obj = spider(trading, 'cftc')
    profile = {'url': 'https://www.cftc.gov/PressRoom/SpeechesTestimony/MichaelSelig/index.htm', 'kind': 'article'}
    speech = {'url': 'https://www.cftc.gov/PressRoom/SpeechesTestimony/opaselig1', 'kind': 'article'}
    obj.cursor['realtime']['pending'] = [profile, speech]
    obj.cursor['document_retries'] = {doc['url']: {'document': doc, 'attempts': 1, 'next_at': obj.run.started_at.timestamp() + 3600} for doc in (profile, speech)}
    list(obj.first_requests())
    assert profile['url'] not in obj.cursor['document_retries']
    assert speech['url'] in obj.cursor['document_retries']
    assert all(d['url'] != profile['url'] for d in obj.cursor['realtime'].get('pending', []))


def test_undated_or_unordered_lists_do_not_stop_early(trading):
    obj = spider(trading, 'treasury')
    url = SOURCES[obj.name].entries[0]
    body = '<h1>News</h1><div><time datetime="2025-01-01"></time><h3><a href="/news/press-releases/sb0001">Old</a></h3></div><a href="/news/press-releases/sb0002">Undated</a><a rel="next" href="?page=2">Next</a>'
    list(obj.parse_listing(response(url, body, lane='realtime')))
    assert obj.cursor['realtime']['next'].endswith('?page=2')
    assert obj.cursor['realtime']['pending'][0]['url'].endswith('sb0002')


def test_failed_document_does_not_block_next_document_or_retry_immediately(trading):
    obj = spider(trading, 'doj')
    docs = [{'url': f'https://www.justice.gov/api/v1/press_releases/{uuid.uuid4()}.json', 'kind': 'doj'} for _ in range(2)]
    obj.cursor['realtime']['pending'] = copy.deepcopy(docs)
    request = Request(docs[0]['url'], meta={'document': docs[0], 'lane': 'realtime'})
    failed = Failure(HttpError(Response(request.url, status=503, request=request)))
    failed.request = request
    following, = obj.on_request_error(failed)
    assert following.url == docs[1]['url']
    assert obj.run.errors and docs[0]['url'] not in obj.cursor.get('checked', {})
    gap = obj.cursor['document_retries'][docs[0]['url']]
    assert gap['document'] == docs[0] and gap['attempts'] == 1
    assert gap['next_at'] > obj.run.started_at.timestamp()


@pytest.mark.parametrize('attempts,due,expected', [(1, False, False), (3, True, False), (1, True, True)])
def test_document_retry_is_bounded_and_survives_rounds(trading, attempts, due, expected):
    obj = spider(trading, 'ofac')
    url = 'https://ofac.treasury.gov/recent-actions/20261104'
    obj.cursor['document_retries'] = {url: {'document': {'url': url, 'kind': 'article'}, 'attempts': attempts, 'next_at': obj.run.started_at.timestamp() + (-60 if due else 3600), 'error': 'HTTP503'}}
    listing = '<h1>Actions</h1><a href="'+url+'">Sanctions</a>'
    requests = list(obj.parse_listing(response(SOURCES[obj.name].entries[0], listing, lane='realtime')))
    assert any(r.url == url for r in requests) is expected
    assert url in obj.cursor['document_retries']


def test_old_pending_cursor_cannot_bypass_exhausted_document_retry(trading):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20261104', 'kind': 'article'}
    obj.cursor['realtime']['pending'] = [doc]
    obj.cursor['document_retries'] = {doc['url']: {'document': doc, 'attempts': 3, 'next_at': 0, 'error': 'unavailable'}}
    requests = obj.advance('realtime')
    assert all(r.url != doc['url'] for r in requests)
    assert doc['url'] in obj.cursor['document_retries']


def test_due_retry_in_old_pending_cursor_is_not_discarded_by_moving_window(trading):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20250101', 'kind': 'article', 'listing_date': '2025-01-01'}
    obj.cursor['realtime']['pending'] = [doc]
    obj.cursor['document_retries'] = {doc['url']: {'document': copy.deepcopy(doc), 'attempts': 1, 'next_at': 0, 'error': 'unavailable'}}
    request, = obj.first_requests()
    assert request.meta['document']['document_retry']
    body = '<h1>Crypto sanctions</h1><main><article class="node--type-ofac-recent-action"><div class="field--name-field-body"><p>New sanctions against a virtual currency exchange institution.</p></div></article></main>'
    async def collect():
        return [item async for item in obj.parse_document(response(request.url, body, **request.meta))]
    items = asyncio.run(collect())
    assert any(isinstance(item, dict) and item['url'] == doc['url'] for item in items)
    assert not obj.cursor['document_retries']
    assert doc['url'] in obj.cursor['checked']


@pytest.mark.parametrize('restored_pending', [False, True])
def test_attachment_retry_outside_realtime_window_fetches_and_completes_pdf(trading, monkeypatch, restored_pending):
    from alphapulse.crawler.spiders import us_official
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20250101', 'kind': 'article', 'listing_date': '2025-01-01'}
    obj.cursor['attachment_retries'] = {doc['url']: {'document': doc, 'attempts': 1, 'next_at': 0}}
    if restored_pending:
        obj.cursor['realtime']['pending'] = [copy.deepcopy(doc)]
    request, = obj.first_requests()
    body = '<h1>Crypto sanctions</h1><main><article class="node--type-ofac-recent-action"><div class="field--name-field-body"><p>New sanctions against a virtual currency exchange institution.</p><a href="/media/123/download">Order</a></div></article></main>'
    async def collect():
        return [item async for item in obj.parse_document(response(request.url, body, **request.meta))]
    attachment, = asyncio.run(collect())
    assert isinstance(attachment, Request) and attachment.url.endswith('/media/123/download')
    assert obj.cursor['attachment_retries'][doc['url']]['attempts'] == 1
    assert doc['url'] not in obj.cursor.get('checked', {})
    monkeypatch.setattr(us_official, 'pdf_text', lambda body: 'The official sanctions order applies to a virtual currency exchange.')
    items = list(obj.parse_attachment(Response(attachment.url, body=b'%PDF-test', request=attachment.replace(meta={**attachment.meta, 'raw_response_id': 13}))))
    item = next(item for item in items if isinstance(item, dict))
    assert item['extra']['content_scope'] == 'article'
    assert item['extra']['attachments'][0]['status'] == 'complete'
    assert not obj.cursor['attachment_retries']
    assert doc['url'] in obj.cursor['checked']


def test_due_attachment_is_queued_alongside_document_retry_without_duplicates(trading):
    obj = spider(trading)
    documents = [{'url': f'https://ofac.treasury.gov/recent-actions/2026110{day}', 'kind': 'article'} for day in (3, 4)]
    obj.cursor['document_retries'] = {documents[0]['url']: {'document': documents[0], 'attempts': 1, 'next_at': 0, 'error': 'HTTP503'}}
    obj.cursor['attachment_retries'] = {doc['url']: {'document': doc, 'attempts': 1, 'next_at': 0} for doc in documents}
    request, = obj.first_requests()
    pending = obj.cursor['realtime']['pending']
    assert [doc['url'] for doc in pending] == [doc['url'] for doc in documents]
    assert request.meta['document']['document_retry'] and request.meta['document']['attachment_retry']
    requests = obj.complete('realtime')
    assert requests[0].url == documents[1]['url'] and requests[0].meta['document']['attachment_retry']


@pytest.mark.parametrize('first_lane', ['realtime', 'history'])
def test_successful_retry_is_not_requeued_by_other_lane(trading, first_lane):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20261104', 'kind': 'article'}
    obj.cursor['document_retries'] = {doc['url']: {'document': doc, 'attempts': 1, 'next_at': 0, 'error': 'HTTP503'}}
    for lane in ('realtime', 'history'):
        obj.cursor[lane]['pending'] = [copy.deepcopy(doc)]
    request, = obj.advance(first_lane)
    body = '<h1>Crypto sanctions</h1><main><article class="node--type-ofac-recent-action"><div class="field--name-field-body"><p>New sanctions against a virtual currency exchange institution.</p></div></article></main>'
    async def collect():
        return [item async for item in obj.parse_document(response(request.url, body, **request.meta))]
    items = asyncio.run(collect())
    assert any(isinstance(item, dict) for item in items)
    assert not obj.cursor['document_retries']
    other_lane = 'history' if first_lane == 'realtime' else 'realtime'
    assert all(request.url != doc['url'] for request in obj.advance(other_lane))


@pytest.mark.parametrize('first_lane', ['realtime', 'history'])
def test_document_retry_is_requested_once_across_both_lanes(trading, first_lane):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20261104', 'kind': 'article'}
    obj.cursor['document_retries'] = {doc['url']: {'document': doc, 'attempts': 1, 'next_at': 0, 'error': 'HTTP503'}}
    for lane in ('realtime', 'history'):
        obj.cursor[lane]['pending'] = [copy.deepcopy(doc)]
    request, = obj.advance(first_lane)
    other_lane = 'history' if first_lane == 'realtime' else 'realtime'
    following = obj.advance(other_lane)
    assert all(r.url != doc['url'] for r in following)
    failure = Failure(HttpError(Response(request.url, status=503, request=request)))
    failure.request = request
    obj.on_request_error(failure)
    assert obj.cursor['document_retries'][doc['url']]['attempts'] == 2
    assert doc['url'] not in obj.cursor.get('checked', {})
    # Completing a concurrent listing must not re-queue the same retry.
    listing = '<h1>Actions</h1><a href="' + doc['url'] + '">Sanctions</a>'
    requests = list(obj.parse_listing(response(SOURCES[obj.name].entries[0], listing, lane=other_lane)))
    assert all(r.url != doc['url'] for r in requests)


def test_concurrent_initial_document_failures_keep_one_attempt_and_all_errors(trading):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20261104', 'kind': 'article'}
    for lane in ('realtime', 'history'):
        obj.cursor[lane]['pending'] = [copy.deepcopy(doc)]
    requests = [obj.advance(lane)[0] for lane in ('realtime', 'history')]
    for request in requests:
        failure = Failure(HttpError(Response(request.url, status=503, request=request)))
        failure.request = request
        obj.on_request_error(failure)
    assert obj.cursor['document_retries'][doc['url']]['attempts'] == 1
    assert len(obj.run.errors) == 2


def test_document_retry_waits_one_hour_after_failure_in_long_round(trading):
    obj = spider(trading)
    obj.run.started_at = datetime.now(UTC) - timedelta(hours=2)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20261104', 'kind': 'article'}
    obj.cursor['realtime']['pending'] = [doc]
    request, = obj.advance('realtime')
    failure = Failure(HttpError(Response(request.url, status=503, request=request)))
    failure.request = request
    before = datetime.now(UTC)
    obj.on_request_error(failure)
    after = datetime.now(UTC)
    due_at = obj.cursor['document_retries'][doc['url']]['next_at']
    assert (before + timedelta(hours=1)).timestamp() <= due_at <= (after + timedelta(hours=1)).timestamp()


def test_pipeline_error_prevents_completed_read_checkpoint(trading):
    obj = spider(trading)
    obj.run.completed_documents_safe = True
    obj._on_item_error(Failure(ValueError('test persistence failure')))
    assert not obj.run.completed_documents_safe
    assert '落库失败' in obj.run.errors[0]


def test_completed_document_error_cannot_remove_next_pending_document(trading, monkeypatch):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20261104', 'kind': 'article'}
    following = {'url': 'https://ofac.treasury.gov/recent-actions/20261103', 'kind': 'article'}
    obj.cursor['realtime']['pending'] = [following]
    async def scheduling_failed(res):
        raise ValueError('next request scheduling failed')
        yield
    monkeypatch.setattr(obj, '_parse_document', scheduling_failed)
    async def collect():
        return [item async for item in obj.parse_document(response(doc['url'], '', document=doc, lane='realtime'))]
    with pytest.raises(ValueError, match='scheduling failed'):
        asyncio.run(collect())
    assert obj.cursor['realtime']['pending'] == [following]
    assert not obj.cursor.get('document_retries')


@pytest.mark.parametrize('safe', [False, True])
def test_failed_round_saves_only_safe_completed_reads_and_explicit_gaps(trading, monkeypatch, safe):
    from alphapulse.crawler.spiders import base
    obj = spider(trading, 'doj')
    source = SimpleNamespace(history_target=obj.run.history_target, history_cursor={'history': {'entry': 0}, 'realtime': {'entry': 0}}, consecutive_failures=0)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def execute(self, *args): pass
        async def get(self, *args, **kwargs): return source
        async def commit(self): pass
    monkeypatch.setattr(base, 'SessionLocal', Session)
    obj.run.errors = ['failed document']
    obj.run.completed_documents_safe = safe
    gap = {'document': {'url': 'https://www.justice.gov/api/v1/press_releases/bad.json', 'kind': 'doj'}, 'attempts': 1, 'next_at': obj.run.started_at.timestamp() + 3600}
    obj.run.official_cursor = {'transport': 'doj-api-v1', 'history': {'entry': 5}, 'realtime': {'entry': 8}, 'checked': {'good': 123}, 'document_retries': {'bad': gap}}
    asyncio.run(obj._finish_run(obj.run))
    assert source.history_cursor['history'] == {'entry': 0}
    assert source.history_cursor['realtime'] == {'entry': 0}
    assert ('checked' in source.history_cursor) is safe
    assert ('document_retries' in source.history_cursor) is safe
    assert ('transport' in source.history_cursor) is safe
    if safe:
        assert source.history_cursor['checked'] == {'good': 123}
        assert 'bad' in source.history_cursor['document_retries']
        restored = spider(trading, 'doj')
        restored.cursor = copy.deepcopy(source.history_cursor)
        restored.first_requests()
        assert restored.cursor['checked'] == {'good': 123}
        assert restored.cursor['document_retries'] == {'bad': gap}


@pytest.mark.parametrize('status', [403, 429])
def test_access_block_stops_round_and_honors_retry_after(trading, status):
    obj = spider(trading, 'cftc')
    doc = {'url': 'https://www.cftc.gov/PressRoom/PressReleases/9321-26', 'kind': 'article'}
    obj.cursor['realtime']['pending'] = [doc]
    request = Request(doc['url'], meta={'document': doc, 'lane': 'realtime'})
    failure = Failure(HttpError(Response(request.url, status=status, headers={'Retry-After': '3600'}, request=request)))
    failure.request = request
    obj.on_request_error(failure)
    assert obj.run.blocked
    assert obj.run.cooldown_until > datetime.now(UTC) + timedelta(minutes=59)
    assert obj.advance('realtime') == []
    assert obj.cursor['realtime']['pending'] == [doc]
    assert not obj.cursor.get('document_retries')


@pytest.mark.parametrize('later_block', ['403', '429', 'listing_challenge', 'document_challenge'])
def test_later_block_does_not_shorten_existing_retry_after(trading, later_block):
    obj = spider(trading, 'cftc')
    doc = {'url': 'https://www.cftc.gov/PressRoom/PressReleases/9321-26', 'kind': 'article'}
    obj.cursor['realtime']['pending'] = [doc]
    request = Request(doc['url'], meta={'document': doc, 'lane': 'realtime'})
    failure = Failure(HttpError(Response(request.url, status=429, headers={'Retry-After': '3600'}, request=request)))
    failure.request = request
    obj.on_request_error(failure)
    until = obj.run.cooldown_until
    if later_block in ('403', '429'):
        failure = Failure(HttpError(Response(request.url, status=int(later_block), request=request)))
        failure.request = request
        obj.on_request_error(failure)
    else:
        body = '<h1>Just a moment...</h1><p>Enable JavaScript and cookies to continue.</p>'
        if later_block == 'listing_challenge':
            with pytest.raises(ValueError, match='验证页面'):
                list(obj.parse_listing(response(SOURCES[obj.name].entries[0], body, lane='history')))
        else:
            async def collect():
                return [item async for item in obj.parse_document(response(doc['url'], body, document=doc, lane='realtime'))]
            assert asyncio.run(collect()) == []
    assert obj.run.blocked and obj.run.cooldown_until == until


@pytest.mark.parametrize('source,title,text,expected', [
    ('doj', 'Man sentenced for bitcoin theft', 'Personal bitcoin theft worth $300', False),
    ('doj', 'Exchange charged', 'Cryptocurrency exchange charged with money laundering', True),
    ('fincen', 'Digital asset rule', 'FinCEN withdraws proposed digital asset rule', True),
    ('whitehouse', 'Tariffs', 'President adjusts import tariffs', True),
    ('treasury', 'Meeting with delegation', 'Greetings and cooperation', False),
    ('ofac', 'New sanctions', 'Sanctions against a financial institution', True),
    ('sec', 'Ethernet equipment', 'Method and hardware', False),
    ('whitehouse', "The President's Budget for Fiscal Year 2027", 'The President submitted his budget request to Congress, proposing nationwide spending and revenue changes.', True),
    ('whitehouse', 'The President’s Budget', 'Annual spending and revenue proposals.', True),
    ('congress', 'Continuing Appropriations Act, 2027', 'This bill provides continuing appropriation for the operations of the United States.', True),
    ('federal_register', 'Adjustments to the Reserve Bank Discount Rates', 'The Federal Reserve increases interest rates charged on loans to depository institutions.', True),
    ('federal_register', 'Reserve Requirements of Depository Institutions', 'The Federal Reserve adjusts reserve requirements.', True),
    ('federal_register', 'Interest on Reserve Balances', 'The Federal Reserve adjusts interest paid on reserve balances.', True),
    ('federal_register', 'Hospital Budget Estimates for Fiscal Year 2027', 'The budget provides funding for hospital operations.', False),
    ('whitehouse', 'Health research funding', 'The department budget supports quantitative research during the fiscal year.', False),
])
def test_selection_boundaries(source, title, text, expected):
    assert bool(selection(source, title, text)) is expected


@pytest.mark.parametrize('url', ['http://ofac.treasury.gov/recent-actions/20261104', 'https://evil.example/a.pdf', 'https://ofac.treasury.gov@evil.example/a', 'https://ofac.treasury.gov/a?api_key=secret'])
def test_official_links_reject_external_and_credentials(url):
    with pytest.raises(ValueError):
        official_url('ofac', url)


def test_dates_do_not_fabricate_publisher_time():
    assert published('2025-01-01') == (datetime(2025, 1, 1, tzinfo=UTC), 'date')
    assert published('2026-11-01T01:30:00-04:00')[0] == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        published('yesterday')


def test_ofac_original_body_and_attachment_boundaries():
    data = article('ofac', '''<h1>Crypto sanctions</h1><main><article class="node--type-ofac-recent-action"><div class="field--name-field-body"><p>New sanctions against a virtual currency exchange institution.</p>
    <a href="/media/123/download">License</a><a href="https://evil.example/a.pdf">external</a></div></article></main>''', 'https://ofac.treasury.gov/recent-actions/20250101')
    assert data['published_at'] == datetime(2025, 1, 1, tzinfo=UTC)
    assert data['extra']['published_at_precision'] == 'date'
    assert len(data['extra']['attachments']) == 2
    assert data['extra']['attachments'][1]['status'] == 'unsupported_host'


def test_live_ofac_body_and_policy_selection():
    import gzip
    from pathlib import Path
    sample = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/us_news/live_selection.json.gz').read_bytes()))
    parsed = article('ofac', sample['ofac']['body'], sample['ofac']['url'])
    assert 'Iran General License FF-1' in parsed['content_html']
    assert "A Part of Treasury's Office" not in parsed['content_html']
    for case in sample['cases']:
        assert bool(selection(case['source_code'], case['title'], case['content_text'])) is case['expected'], case['title']


def test_unrelated_failed_attachment_is_not_news(trading):
    obj = spider(trading, 'doj')
    obj.run.history_deadline = time.monotonic() - 1
    doc = {'url': 'https://www.justice.gov/api/v1/press_releases/x.json', 'kind': 'doj'}
    obj.cursor['history']['pending'] = [doc]
    parsed = {'url': doc['url'], 'title': 'Federal prosecutor receives national award', 'external_id': 'award', 'content_html': '<p>The department recognized superior performance in Indian Country.</p>', 'published_at': datetime(2026, 11, 4, tzinfo=UTC), 'raw_response_id': 12, 'retry_document': doc,
              'extra': {'attachments': [{'url': 'https://www.justice.gov/media/1/dl', 'status': 'unavailable'}]}}
    assert list(obj.attach_or_finish(parsed, 'history')) == []
    assert obj.run.stats['unclassified_attachment_gaps'] == 1
    assert obj.cursor['attachment_retries'][doc['url']]['attempts'] == 1


@pytest.mark.parametrize('content_type,body', [('image/png', b'\x89PNG\r\n\x1a\n'), ('image/jpeg', b'\xff\xd8\xff'), ('image/gif', b'GIF89a')])
@pytest.mark.parametrize('relevant', [True, False])
def test_image_attachment_preserves_unknown_text_gap(trading, monkeypatch, content_type, body, relevant):
    from alphapulse.crawler.spiders import us_official
    obj = spider(trading, 'doj')
    obj.run.history_deadline = time.monotonic() - 1
    url = 'https://www.justice.gov/media/1/dl'
    doc = {'url': 'https://www.justice.gov/api/v1/press_releases/x.json', 'kind': 'doj'}
    obj.cursor['history']['pending'] = [doc]
    parsed = {'url': doc['url'], 'title': 'Cryptocurrency exchange charged' if relevant else 'Department announces an enforcement action', 'external_id': 'case', 'content_html': '<p>The cryptocurrency exchange was charged with money laundering.</p>' if relevant else '<p>See the attached consent order for the findings and penalty.</p>', 'published_at': datetime(2026, 11, 4, tzinfo=UTC), 'raw_response_id': 12, 'retry_document': doc,
              'extra': {'attachments': [{'url': url, 'title': 'Consent Order', 'status': 'pending'}]}}
    monkeypatch.setattr(us_official, 'pdf_text', lambda body: pytest.fail('Images must not reach the PDF parser'))
    res = Response(url, body=body, headers={'Content-Type': content_type}, request=Request(url, meta={'lane': 'history', 'parsed': parsed, 'raw_response_id': 13}))
    items = list(obj.parse_attachment(res))
    if relevant:
        item, = items
        assert item['extra']['content_scope'] == 'partial'
    else:
        assert items == []
        assert obj.run.stats['unclassified_attachment_gaps'] == 1
    attachment = parsed['extra']['attachments'][0]
    assert attachment['status'] == 'unavailable'
    assert '图片' in attachment['error']
    assert attachment['error'] in obj.cursor['attachment_retries'][doc['url']]['error']
    assert url in obj.cursor['attachment_retries'][doc['url']]['error']
    assert attachment['raw_response_id'] == 13
    assert obj.cursor['attachment_retries'][doc['url']]['attempts'] == 1


def test_source_api_keeps_attachment_gaps_visible_across_rounds(trading, monkeypatch):
    from unittest.mock import AsyncMock
    from alphapulse.api import crawler
    from alphapulse.models import Source

    source = Source(code='doj', name='DOJ', list_url=SOURCES['doj'].entries[0], enabled=False, interval_seconds=300, backfill_hours=72,
                    consecutive_failures=0, last_success_at=None, last_error=None, paused_until=None, history_cursor={'attachment_retries': {'a': {'attempts': 1}, 'b': {'attempts': 2}, 'c': {'attempts': 3}}, 'document_retries': {'d': {'attempts': 1}, 'e': {'attempts': 3}}, 'bill_retries': {'f': {'attempts': 1}, 'g': {'attempts': 3}}},
                    history_target=None, history_position=None, history_done=False)
    latest = SimpleNamespace(source_code='doj', status='success', stats={'discovered': 2, 'attachment_gaps': 999})

    class Session:
        async def scalars(self, statement):
            rows = [source] if statement.column_descriptions[0]['entity'] is Source else [latest]
            return SimpleNamespace(all=lambda: rows)
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: []) if 'news_items' in str(statement) else []
        async def scalar(self, statement):
            return 1234

    monkeypatch.setattr(crawler.redis, 'hgetall', AsyncMock(return_value={}))
    monkeypatch.setattr(crawler, 'calendar_sources', AsyncMock(return_value=[]))
    for stats in ({'discovered': 2, 'attachment_gaps': 999, 'unresolved_bills': 999}, {}):
        latest.stats = stats
        result = asyncio.run(crawler.list_sources(Session())).data.sources[0]
        assert result.collection_stats['attachment_gaps'] == 3
        assert result.collection_stats['attachment_retry_pending'] == 2
        assert result.collection_stats['attachment_retry_exhausted'] == 1
        assert result.collection_stats['document_gaps'] == 2
        assert result.collection_stats['document_retry_pending'] == 1
        assert result.collection_stats['document_retry_exhausted'] == 1
        assert result.collection_stats['unresolved_bills'] == 2
        assert result.collection_stats['bill_retry_pending'] == 1
        assert result.collection_stats['bill_retry_exhausted'] == 1
        latest.status = 'running'
        running = asyncio.run(crawler.list_sources(Session())).data.sources[0]
        assert running.collection_stats['unresolved_bills'] == 2
        assert running.collection_stats['round_running'] == 1
        latest.status = 'success'
        assert result.collection_stats['round_running'] == 0
    source.history_cursor['attachment_retries'].pop('a')
    result = asyncio.run(crawler.list_sources(Session())).data.sources[0]
    assert result.collection_stats['attachment_gaps'] == 2
    assert result.collection_stats['attachment_retry_pending'] == 1
    source.history_cursor = {}
    latest.stats = {'attachment_gaps': 999}
    result = asyncio.run(crawler.list_sources(Session())).data.sources[0]
    assert result.collection_stats['attachment_gaps'] == 0
    assert result.collection_stats['attachment_retry_exhausted'] == 0
    assert result.collection_stats['document_gaps'] == 0
    assert result.collection_stats['unresolved_bills'] == 0
    assert result.collection_stats['bill_retry_pending'] == 0
    assert result.collection_stats['bill_retry_exhausted'] == 0
    latest.status = 'running'
    result = asyncio.run(crawler.list_sources(Session())).data.sources[0]
    assert result.collection_stats['round_running'] == 1


def test_challenge_is_failure_not_empty_success(trading):
    obj = spider(trading)
    with pytest.raises(ValueError, match='未识别'):
        list(obj.parse_listing(response(SOURCES['ofac'].entries[0], '<html>Please wait</html>', lane='history')))


def test_history_pending_documents_survive_deadline_and_resume(trading):
    obj = spider(trading)
    doc = {'url': 'https://ofac.treasury.gov/recent-actions/20250101', 'kind': 'article'}
    obj.cursor['history']['pending'] = [doc]
    obj.run.history_deadline = time.monotonic() - 1
    assert obj.advance('history') == []
    saved = copy.deepcopy(obj.cursor)
    resumed = spider(trading)
    resumed.cursor = saved
    request, = resumed.advance('history')
    assert request.url == doc['url'] and resumed.cursor['history']['pending'] == [doc]
    assert not resumed.run.history_done


@pytest.mark.parametrize('count', [1999, 2000, 2001])
def test_register_oversized_day_uses_xml(trading, count):
    obj = spider(trading, 'federal_register')
    obj.cursor['history']['day'] = '2025-01-01'
    req, = obj.parse_listing(response(SOURCES[obj.name].entries[0], json.dumps({'count': count, 'results': [{'document_number': '2025-1'}]}), lane='history'))
    assert ('govinfo.gov' in req.url) is (count > 2000)
    assert obj.cursor['history']['day'] == '2025-01-01'
    assert not obj.run.history_done


def test_keys_only_sent_to_congress_api(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test-key', crawler_contact_email='public@example.invalid'))
    obj = spider(trading, 'congress')
    assert obj.request_for('https://api.congress.gov/v3/bill', lambda x: x, 'history').headers['X-Api-Key'] == b'test-key'
    assert 'X-Api-Key' not in obj.request_for('https://www.congress.gov/text', lambda x: x, 'history').headers


def test_register_body_and_pdf_are_part_of_the_same_package(trading):
    obj = spider(trading, 'federal_register')
    doc = {'url': 'https://www.federalregister.gov/api/v1/documents/2025-1.json', 'kind': 'register'}
    obj.cursor['history']['pending'] = [doc]
    res = response(doc['url'], json.dumps({'title': 'Digital asset rule', 'publication_date': '2025-01-01', 'document_number': '2025-1', 'body_html_url': 'https://www.federalregister.gov/documents/full_text/html/2025/01/01/2025-1.html', 'html_url': 'https://www.federalregister.gov/documents/2025/01/01/2025-1/a', 'pdf_url': 'https://www.govinfo.gov/a.pdf'}), lane='history', document=doc)
    async def collect():
        return [v async for v in obj.parse_document(res)]
    request, = asyncio.run(collect())
    assert request.meta['document']['pdf'] == 'https://www.govinfo.gov/a.pdf'


def test_summary_scope_tests_do_not_hide_fed_enrichment(trading):
    from alphapulse.crawler.spiders.rss import FedSpider
    from alphapulse.crawler.spiders.us_official import USOfficialSpider
    assert issubclass(FedSpider, USOfficialSpider)
    assert len(SOURCES['fed'].entries) >= 8


def test_fed_entries_match_real_archives_and_discover_documents(trading):
    import gzip
    from pathlib import Path
    samples = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/us_news/fed_archives.json.gz').read_bytes()))
    for sample in samples:
        assert sample['url'] in SOURCES['fed'].entries
        obj = spider(trading, 'fed')
        obj.cursor['history']['entry'] = SOURCES['fed'].entries.index(sample['url'])
        request, = obj.advance('history')
        assert request.url == sample['url']
        document, = obj.parse_listing(response(request.url, sample['body'], lane='history'))
        assert document.meta['document']['kind'] == 'article'
        assert document.url not in SOURCES['fed'].entries
        assert document.url.startswith('https://www.federalreserve.gov/')
        assert obj.run.stats['discovered'] > 0


def test_fed_realtime_completes_all_real_discovery_entries(trading, monkeypatch):
    from dataclasses import replace
    import gzip
    from pathlib import Path
    samples = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/us_news/fed_archives.json.gz').read_bytes()))
    bodies = {s['url']: s['body'] for s in samples}
    monkeypatch.setitem(SOURCES, 'fed', replace(SOURCES['fed'], entries=tuple(bodies)))
    obj = spider(trading, 'fed')
    requests = obj.first_requests()
    visited = []
    while requests:
        request = requests.pop()
        if request.meta.get('document'):
            assert '2024' not in request.url
            requests.extend(obj.complete('realtime'))
        else:
            assert request.url not in visited
            visited.append(request.url)
            requests.extend(obj.parse_listing(response(request.url, bodies[request.url], lane='realtime')))
    assert visited == list(SOURCES['fed'].entries)
    assert obj.cursor['realtime'] == {'entry': 0}
    assert not obj.run.errors


@pytest.mark.parametrize('lane', ['history', 'realtime'])
def test_fed_discovery_respects_old_and_boundary_dates(trading, lane):
    obj = spider(trading, 'fed')
    obj.run.cutoff = datetime(2026, 11, 1, 15, tzinfo=UTC)
    body = '<h1>FOMC</h1>' + ''.join(
        f'<a href="/newsevents/pressreleases/monetary{day}a1.htm">Note</a>'
        for day in ('20240131', '20251031', '20261101')
    )
    list(obj.parse_listing(response('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm', body, lane=lane)))
    pending = obj.cursor[lane]['pending']
    assert all('20240131' not in doc['url'] for doc in pending)
    assert any('20261101' in doc['url'] for doc in pending)
    assert any('20251031' in doc['url'] for doc in pending) is (lane == 'history')


@pytest.mark.parametrize('lane', ['history', 'realtime'])
def test_fed_minutes_use_official_release_day_instead_of_meeting_day(trading, monkeypatch, lane):
    import gzip
    from pathlib import Path
    samples = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/us_news/fed_archives.json.gz').read_bytes()))
    sample = next(s for s in samples if 'fomccalendars' in s['url'])
    obj = spider(trading, 'fed')
    obj.run.cutoff = obj.run.history_target = datetime(2026, 2, 18, 15, tzinfo=UTC)
    list(obj.parse_listing(response(sample['url'], sample['body'], lane=lane)))
    doc = next(d for d in obj.cursor[lane]['pending'] if 'fomcminutes20260128.htm' in d['url'])
    assert doc['release_date'] == '2026-02-18'
    assert doc['release_date_source'] == sample['url']
    bodies = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/us_news/fed_materials.json.gz').read_bytes()))
    body = next(s['body'] for s in bodies if s['url'] == doc['url'])
    from alphapulse.crawler.spiders import us_official
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def scalar(self, query): return None
    monkeypatch.setattr(us_official, 'SessionLocal', Session)
    monkeypatch.setattr(obj, 'attach_or_finish', lambda parsed, lane: iter([parsed]))
    async def collect():
        return [p async for p in obj.parse_document(response(doc['url'], body, lane=lane, document=doc))]
    parsed, = asyncio.run(collect())
    assert parsed['published_at'] == datetime(2026, 2, 18, tzinfo=UTC)
    assert parsed['extra']['published_at_basis'] == 'official_calendar_release'
    assert parsed['extra']['published_at_source'] == sample['url']


def test_fed_minutes_missing_release_date_fail_explicitly(trading):
    obj = spider(trading, 'fed')
    body = '<h1>FOMC</h1><a href="/monetarypolicy/fomcminutes20260128.htm">HTML</a>'
    with pytest.raises(ValueError, match='官方发布日期'):
        list(obj.parse_listing(response(SOURCES['fed'].entries[-2], body, lane='history')))


def test_fed_real_policy_bodies_have_titles_and_publication_dates():
    import gzip
    from pathlib import Path
    samples = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/us_news/fed_materials.json.gz').read_bytes()))
    for sample in samples:
        is_minutes = '/fomcminutes' in sample['url']
        if is_minutes:
            with pytest.raises(ValueError, match='官方日历发布日期'):
                article('fed', sample['body'], sample['url'])
            with pytest.raises(ValueError, match='可识别的发布时间'):
                article('fed', sample['body'], sample['url'], release_date='invalid')
        parsed = article('fed', sample['body'], sample['url'], release_date=sample['published_date'] if is_minutes else None)
        assert parsed['title'] == sample['title']
        assert parsed['published_at'].date().isoformat() == sample['published_date']
        assert parsed['extra']['published_at_precision'] == 'date'
        assert len(parsed['content_html']) > 1000


def test_fifty_real_public_documents_parse_without_io():
    import gzip
    from pathlib import Path
    samples = json.load(gzip.open(Path(__file__).parent / 'fixtures/us_news/public_samples.json.gz', 'rt'))
    assert len(samples) == 50
    assert len({s['source'] for s in samples}) == 8
    for sample in samples:
        parsed = article(sample['source'], sample['body'], sample['url'])
        assert parsed['title'] == sample['expected_title']
        assert len(parsed['content_html']) > 40
        assert parsed['published_at'].year >= 2025
        assert parsed['extra']['published_at_precision'] in ('date', 'datetime')


def test_daily_register_partitions_resume_without_marking_done(trading):
    obj = spider(trading, 'federal_register')
    obj.cursor['history']['day'] = '2025-01-02'
    requests = list(obj.parse_listing(response(SOURCES[obj.name].entries[0], json.dumps({'count': 0, 'results': []}), lane='history')))
    assert len(requests) == 1
    assert obj.cursor['history']['day'] == '2025-01-01'
    assert not obj.run.history_done


def test_h41_uses_publisher_release_dates_not_assumed_thursdays(trading):
    obj = spider(trading, 'fed')
    data = [{'yearValue': '2025', 'Months': [{'MonthName': 'January', 'Dates': ['20250103', '20250109']}]}]
    req, = obj.parse_listing(response('https://www.federalreserve.gov/releases/h41/releaseDates.json', json.dumps(data), lane='history'))
    assert req.url == 'https://www.federalreserve.gov/releases/h41/20250109/'
    assert obj.cursor['history']['pending'][1]['url'].endswith('20250103/')


def test_attachment_failure_is_visible_and_keeps_a_bounded_retry(trading):
    obj = spider(trading)
    obj.run.history_deadline = time.monotonic() - 1
    url = 'https://ofac.treasury.gov/recent-actions/20250101'
    doc = {'url': url, 'kind': 'article'}
    obj.cursor['history']['pending'] = [doc]
    parsed = {'url': url, 'title': 'Crypto sanctions', 'external_id': 'x', 'content_html': '<p>Virtual currency exchange sanctions.</p>', 'published_at': datetime(2025, 1, 1, tzinfo=UTC), 'raw_response_id': 12, 'retry_document': doc,
              'extra': {'attachments': [{'url': 'https://ofac.treasury.gov/a.pdf', 'title': 'License', 'status': 'unavailable'}]}}
    before = datetime.now(UTC)
    item, = obj.attach_or_finish(parsed, 'history')
    after = datetime.now(UTC)
    assert item['extra']['content_scope'] == 'partial'
    assert obj.cursor['attachment_retries'][url]['attempts'] == 1
    assert obj.cursor['attachment_retries'][url]['document'] == doc
    assert (before + timedelta(hours=1)).timestamp() <= obj.cursor['attachment_retries'][url]['next_at'] <= (after + timedelta(hours=1)).timestamp()


@pytest.mark.parametrize('canceled,failed', [(False, False), (True, False), (False, True)])
def test_checkpoint_does_not_advance_failed_or_canceled_history(trading, monkeypatch, canceled, failed):
    from alphapulse.crawler.spiders import base
    now = datetime.now(UTC)
    target = now - timedelta(days=300)
    source = SimpleNamespace(history_target=None if canceled else target, history_cursor={'history': {'entry': 0}}, consecutive_failures=0)
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, *args):
            pass
        async def get(self, *args, **kwargs):
            return source
        async def commit(self):
            pass
    monkeypatch.setattr(base, 'SessionLocal', Session)
    obj = spider(trading)
    run = base.RunState(uuid.uuid4(), 'schedule', now, target, history_target=target, history_position=now, official_cursor={'history': {'entry': 5}, 'realtime': {'entry': 2}})
    if failed:
        run.errors.append('write failed')
    asyncio.run(obj._finish_run(run))
    assert source.history_cursor['history']['entry'] == (0 if canceled or failed else 5)


def test_congress_action_links_use_public_bill_type(trading):
    from alphapulse.crawler.spiders.us_official import congress_bill_url
    assert congress_bill_url('https://api.congress.gov/v3/bill/119/hr/1') == 'https://www.congress.gov/bill/119th-congress/house-bill/1'
    assert congress_bill_url('https://api.congress.gov/v3/bill/119/s/1582') == 'https://www.congress.gov/bill/119th-congress/senate-bill/1582'


def test_tick_during_active_round_does_not_replace_its_cursors(trading):
    obj = spider(trading)
    obj.crawler = SimpleNamespace(engine=SimpleNamespace(spider_is_idle=lambda: False))
    before = copy.deepcopy(obj.cursor)
    assert obj.make_request_from_data(json.dumps({'trigger': 'schedule', 'official_cursor': {'history': {'entry': 99}}}).encode()) == []
    assert obj.cursor == before


def test_realtime_keeps_next_page(trading):
    obj = spider(trading)
    url = SOURCES['ofac'].entries[0]
    list(obj.parse_listing(response(url, '<h1>Recent actions</h1><a href="/recent-actions/20261104">Crypto</a><a rel="next" href="?page=1">Next</a>', lane='realtime')))
    assert obj.cursor['realtime']['next'] == url + '?page=1'


@pytest.mark.parametrize('body', ['', '<h1>Just a moment...</h1><p>Enable JavaScript and cookies to continue.</p>'])
def test_text_challenge_never_emits_or_advances(trading, body):
    obj = spider(trading, 'federal_register')
    doc = {'url': 'https://www.federalregister.gov/body', 'kind': 'text', 'title': 'Digital asset regulation', 'at': '2026-11-04'}
    obj.cursor['history']['pending'] = [doc]
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], body, lane='history', document=doc))]
    with pytest.raises(ValueError):
        asyncio.run(collect())
    assert obj.cursor['history']['pending'] == [doc]


def test_genius_bill_requires_evidence_before_exclusion(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    doc = {'url': 'https://api.congress.gov/v3/bill/119/s/1582', 'kind': 'bill'}
    obj.cursor['history']['pending'] = [doc]
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps({'bill': {'title': 'GENIUS Act', 'policyArea': {'name': 'Finance and Financial Sector'}}}), lane='history', document=doc))]
    req, = asyncio.run(collect())
    assert '/subjects?' in req.url
    assert not obj.run.stats.get('excluded')


def test_real_titles_are_article_headlines():
    import gzip
    from pathlib import Path
    samples = json.load(gzip.open(Path(__file__).parent / 'fixtures/us_news/public_samples.json.gz', 'rt'))
    for s in samples:
        if s['source'] == 'fed' and '2026-press.htm' in s['url']:
            continue
        title = article(s['source'], s['body'], s['url'])['title']
        assert title not in ('Board of Governors of the Federal Reserve System', 'U.S. Department of the Treasury')
        assert not title.startswith('Release Number ')


@pytest.mark.parametrize('enabled,history_enabled,save_failed', [(True, True, False), (False, True, False), (True, False, False), (True, True, True)])
def test_round_waits_for_commit_and_reads_fresh_state(trading, monkeypatch, enabled, history_enabled, save_failed):
    from alphapulse.crawler.spiders import us_official
    fresh = {'history': {'entry': 0, 'target': '2025-01-01T00:00:00+00:00', 'pending': [{'url': 'https://ofac.treasury.gov/recent-actions/20250101', 'kind': 'article'}]}}
    source = SimpleNamespace(enabled=enabled, paused_until=None, history_cursor=fresh, history_target=datetime(2025, 1, 1, tzinfo=UTC) if history_enabled else None, history_done=False, history_position=None, backfill_hours=72)
    reads = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args):
            reads.append(True)
            return source
    monkeypatch.setattr(us_official, 'SessionLocal', Session)
    async def run():
        obj = spider(trading)
        obj.run = None
        obj.crawler = SimpleNamespace(engine=SimpleNamespace(crawl=reads.append))
        obj._insert_run = lambda run: asyncio.sleep(0)
        saved = asyncio.Event()
        async def finish():
            await saved.wait()
            if save_failed: raise RuntimeError('test write failure')
        obj.finish_task = asyncio.create_task(finish())
        boot = asyncio.create_task(obj.start_round('schedule'))
        await asyncio.sleep(0)
        assert not reads
        saved.set()
        await boot
        requests = [r for r in reads if isinstance(r, Request)]
        if enabled and not save_failed:
            assert bool([r for r in requests if r.meta['lane'] == 'history']) is history_enabled
            if history_enabled:
                assert requests[-1].url.endswith('20250101')
        else:
            assert not requests and obj.run is None
        await asyncio.gather(*obj._tasks)
    asyncio.run(run())


def test_register_realtime_finishes_all_pages_before_changing_day(trading):
    obj = spider(trading, 'federal_register')
    obj.cursor['realtime']['day'] = '2026-11-04'
    url = SOURCES[obj.name].entries[0]
    list(obj.parse_listing(response(url, json.dumps({'count': 1001, 'results': [{'document_number': '2026-1234'}], 'next_page_url': url + '?page=2'}), lane='realtime')))
    assert obj.cursor['realtime']['day'] == '2026-11-04'
    obj.cursor['realtime']['pending'] = []
    req, = obj.advance('realtime')
    assert req.url.endswith('?page=2')
    list(obj.parse_listing(response(req.url, json.dumps({'count': 1001, 'results': [{'document_number': '2026-1235'}]}), lane='realtime')))
    assert obj.cursor['realtime']['day'] == '2026-11-04'
    obj.cursor['realtime']['pending'] = []
    obj.advance('realtime')
    assert obj.cursor['realtime']['day'] == '2026-11-03'


def test_pagination_cycle_does_not_claim_success(trading):
    obj = spider(trading)
    url = SOURCES[obj.name].entries[0]
    obj.cursor['history']['visited_pages'] = [url]
    with pytest.raises(ValueError, match='循环'):
        list(obj.parse_listing(response(url + '?page=2', f'<h1>News</h1><a href="/recent-actions/20261104">News</a><a rel="next" href="{url}">Next</a>', lane='history')))
    assert not obj.run.history_done


def test_fragment_is_valid_without_page_heading(trading):
    obj = spider(trading, 'federal_register')
    obj.run.history_deadline = time.monotonic() - 1
    doc = {'url': 'https://www.federalregister.gov/body', 'kind': 'text', 'title': 'Digital asset regulation', 'at': '2026-11-04'}
    obj.cursor['history']['pending'] = [doc]
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], '<p>This digital asset regulation establishes requirements for stablecoin issuers.</p>', lane='history', document=doc))]
    item, = asyncio.run(collect())
    assert item['extra']['content_scope'] == 'article'


@pytest.mark.parametrize('undated', [False, True])
def test_bill_full_text_can_supply_relevance_and_resume(trading, monkeypatch, undated):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    base = 'https://api.congress.gov/v3/bill/119/s/1582'
    obj.cursor['history']['pending'] = [{'url': base, 'kind': 'bill'}]
    async def parse(body):
        doc = obj.cursor['history']['pending'][0]
        return [v async for v in obj.parse_document(response(doc['url'], body, lane='history', document=doc))]
    asyncio.run(parse(json.dumps({'bill': {'title': 'GENIUS Act', 'updateDate': '2026-11-04'}})))
    asyncio.run(parse(json.dumps({'subjects': {'legislativeSubjects': []}})))
    asyncio.run(parse(json.dumps({'summaries': []})))
    versions = [{'type': 'Enrolled', 'date': '2026-11-04', 'formats': [{'type': 'Formatted Text', 'url': 'https://www.congress.gov/text'}]}]
    if undated:
        versions.insert(0, {'type': 'Enrolled Bill', 'date': None, 'formats': []})
    asyncio.run(parse(json.dumps({'textVersions': versions})))
    obj.cursor = json.loads(json.dumps(obj.cursor))
    output = asyncio.run(parse('<p>The bill establishes the regulatory framework for payment stablecoins and digital assets.</p>'))
    assert output[0]['title'] == 'GENIUS Act — Enrolled'
    assert '/actions?' in output[1].url
    output = asyncio.run(parse(json.dumps({'actions': [{'actionDate': '2026-11-04', 'text': 'Became law'}]})))
    assert output[0]['extra']['content_scope'] == 'official_action'
    if undated:
        assert obj.cursor['bill_retries'][base]['error'] == 'Enrolled Bill 缺少官方日期'
    else:
        assert not obj.cursor.get('bill_retries')


def test_missing_bill_material_is_a_retryable_gap(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    base = 'https://api.congress.gov/v3/bill/119/s/1582'
    obj.cursor['history']['pending'] = [{'url': base, 'kind': 'bill'}]
    async def parse(data):
        doc = obj.cursor['history']['pending'][0]
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps(data), lane='history', document=doc))]
    asyncio.run(parse({'bill': {'title': 'GENIUS Act'}}))
    asyncio.run(parse({'subjects': {}}))
    asyncio.run(parse({'summaries': []}))
    before = datetime.now(UTC)
    asyncio.run(parse({'textVersions': []}))
    after = datetime.now(UTC)
    assert obj.cursor['bill_retries'][base]['attempts'] == 1
    assert obj.run.stats['unresolved_bills'] == 1
    assert not obj.run.stats.get('excluded')
    assert obj.cursor['bill_retries'][base]['error'] == '没有可用的官方正文'
    assert (before + timedelta(hours=1)).timestamp() <= obj.cursor['bill_retries'][base]['next_at'] <= (after + timedelta(hours=1)).timestamp()


@pytest.mark.parametrize('attempts,version,expected_kind', [(3, 'v1', 'listing'), (1, 'v1', 'listing'), (3, 'v2', 'subjects')])
def test_bill_retries_are_bounded_until_official_update(trading, monkeypatch, attempts, version, expected_kind):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    url = 'https://api.congress.gov/v3/bill/119/s/1582'
    doc = {'url': url, 'kind': 'bill'}
    obj.cursor['history']['pending'] = [doc]
    obj.cursor['bill_retries'] = {url: {'version': 'v1', 'attempts': attempts, 'next_at': (obj.run.started_at + timedelta(minutes=30)).timestamp()}}
    async def collect():
        return [v async for v in obj.parse_document(response(url, json.dumps({'bill': {'title': 'GENIUS Act', 'updateDate': version}}), lane='history', document=doc))]
    req, = asyncio.run(collect())
    assert ('/subjects?' in req.url) is (expected_kind == 'subjects')


def test_fed_archive_is_not_discovered_as_article(trading):
    obj = spider(trading, 'fed')
    url = SOURCES['fed'].entries[0]
    req, = obj.parse_listing(response(url, '<h1>Press releases</h1><a href="2026-press.htm#content">Archive</a><a href="monetary20261104a.htm">News</a>', lane='history'))
    assert req.url.endswith('monetary20261104a.htm')


def test_ofac_entire_old_page_can_end_realtime_window(trading):
    obj = spider(trading)
    url = SOURCES['ofac'].entries[0]
    list(obj.parse_listing(response(url, '<h1>Recent actions</h1><a href="/recent-actions/20250101">Old</a><a rel="next" href="?page=2">Next</a>', lane='realtime')))
    assert obj.cursor['realtime']['next'] is None


@pytest.mark.parametrize('body', ['<title>Page not found</title><p>The requested official document could not be located.</p>', '<?xml version="1.0"?><bill><section>Digital asset regulation establishes requirements for banks.'])
def test_error_html_and_malformed_xml_are_rejected(body):
    from alphapulse.core.us_news import validate_body
    with pytest.raises(ValueError):
        validate_body(body)


def test_later_bill_version_recalls_earlier_version(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    base = 'https://api.congress.gov/v3/bill/119/s/1582'
    first = {'url': 'https://www.congress.gov/first', 'kind': 'text', 'title': 'GENIUS Act', 'at': '2026-11-04', 'bill': base}
    second = {**first, 'url': 'https://www.congress.gov/second'}
    obj.cursor['history'].update(bill={'url': base, 'reasons': [], 'title': 'GENIUS Act', 'bodies': 0}, pending=[first, second])
    async def parse(doc, body):
        return [v async for v in obj.parse_document(response(doc['url'], body, lane='history', document=doc))]
    output = asyncio.run(parse(first, '<p>This legislation establishes new administrative requirements for federal agencies.</p>'))
    assert len(output) == 1 and isinstance(output[0], Request)
    output = asyncio.run(parse(second, '<p>The legislation creates a regulatory framework for stablecoins and digital assets.</p>'))
    assert output[0]['extra']['selection_reasons']
    assert output[1].url == first['url']
    obj.run.history_deadline = time.monotonic() - 1
    output = asyncio.run(parse(first, '<p>This legislation establishes new administrative requirements for federal agencies.</p>'))
    assert output[0]['title'] == 'GENIUS Act'


def test_bill_retry_keeps_historical_text_outside_realtime_window(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    base = 'https://api.congress.gov/v3/bill/119/s/1582'
    doc = {'url': 'https://www.congress.gov/old', 'kind': 'text', 'title': 'GENIUS Act', 'at': '2025-01-01', 'bill': base}
    obj.cursor['realtime'].update(bill={'url': base, 'title': 'GENIUS Act', 'reasons': ['crypto_policy'], 'bodies': 0, 'retry': True}, pending=[doc])
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], '<p>The law establishes requirements for stablecoin issuers and digital assets.</p>', lane='realtime', document=doc))]
    assert asyncio.run(collect())[0]['published_at'].year == 2025


def test_failed_checkpoint_save_recovers_on_later_trigger(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    source = SimpleNamespace(enabled=True, paused_until=None, history_cursor={}, history_target=None, history_done=False, backfill_hours=72)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args): return source
    monkeypatch.setattr(us_official, 'SessionLocal', Session)
    async def check():
        obj = spider(trading)
        requests, attempts = [], []
        obj.crawler = SimpleNamespace(engine=SimpleNamespace(crawl=requests.append))
        obj._insert_run = lambda run: asyncio.sleep(0)
        original = obj.run
        async def finish(run):
            attempts.append(run)
            if len(attempts) == 1:
                raise RuntimeError('temporary checkpoint failure')
        obj._finish_run = finish
        obj._close_run()
        await asyncio.gather(obj.finish_task, return_exceptions=True)
        await obj.start_round('schedule')
        assert attempts == [original, original]
        assert requests and obj.run is not None
        await asyncio.gather(*obj._tasks)
    asyncio.run(check())


def test_queued_tick_obeys_newly_saved_backoff(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    source = SimpleNamespace(enabled=True, paused_until=datetime.now(UTC) + timedelta(minutes=5), history_cursor={}, history_target=None, history_done=False, backfill_hours=72)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args): return source
    monkeypatch.setattr(us_official, 'SessionLocal', Session)
    async def check():
        obj = spider(trading)
        obj.run = None
        requests = []
        obj.crawler = SimpleNamespace(engine=SimpleNamespace(crawl=requests.append))
        obj._insert_run = lambda run: asyncio.sleep(0)
        await obj.start_round('schedule')
        assert not requests and obj.run is None
        await asyncio.gather(*obj._tasks)
    asyncio.run(check())


def test_checkpoint_retry_budget_preserves_original_round(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'SessionLocal', lambda: pytest.fail('Uncommitted checkpoint must block source reads'))
    async def check():
        obj = spider(trading)
        original, attempts = obj.run, []
        async def fail(run):
            attempts.append(run)
            raise RuntimeError('persistent checkpoint failure')
        obj._finish_run = fail
        obj._close_run()
        await asyncio.gather(obj.finish_task, return_exceptions=True)
        for _ in range(5):
            await obj.start_round('schedule')
        assert attempts == [original] * 3
        assert obj.run is None and not obj._starting
    asyncio.run(check())


@pytest.mark.parametrize('failure', ['', 'count', 'date', 'malformed'])
def test_register_xml_validates_and_resumes(trading, failure):
    obj = spider(trading, 'federal_register')
    doc = {'url': 'https://www.govinfo.gov/issue.xml', 'kind': 'register_issue', 'day': '2025-01-01', 'count': 2001}
    obj.cursor['history'].update(day=doc['day'], pending=[doc])
    xml = '<FEDREG><DATE>January 1, 2025</DATE>' + ''.join(f'<FRDOC>[FR Doc. 2025-{n} Filed 1-1-25]</FRDOC>' for n in range(2001)) + '<FRDOC>[FR Doc. 2025-0 Filed 1-1-25]</FRDOC></FEDREG>'
    if failure == 'count': doc['count'] += 1
    if failure == 'date': doc['day'] = '2025-01-02'
    if failure == 'malformed': xml = xml[:-10]
    before = copy.deepcopy(obj.cursor)
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], xml, lane='history', document=doc))]
    if failure:
        from lxml import etree
        with pytest.raises((ValueError, etree.XMLSyntaxError)):
            asyncio.run(collect())
        assert obj.cursor == before
    else:
        req, = asyncio.run(collect())
        assert len(obj.cursor['history']['pending']) == 2001
        assert req.meta['document']['kind'] == 'register'
        assert obj.cursor['history']['day'] == '2025-01-01'
        obj.run.history_deadline = time.monotonic() - 1
        assert not obj.complete('history')
        resumed = spider(trading, 'federal_register')
        resumed.cursor = json.loads(json.dumps(obj.cursor))
        assert resumed.advance('history')[0].meta['document'] == obj.cursor['history']['pending'][0]


def test_doj_api_pagination_and_old_day_stop(trading):
    obj = spider(trading, 'doj')
    url = SOURCES['doj'].entries[0]
    def data(day):
        return {'metadata': {'responseInfo': {'status': 200}, 'resultset': {'count': '100', 'page': 0, 'pagesize': 50}}, 'results': [{'uuid': str(uuid.uuid4()), 'date': str(int(day.timestamp()))}]}
    req, = obj.parse_listing(response(url, json.dumps(data(obj.run.started_at)), lane='realtime'))
    assert req.meta['document']['kind'] == 'doj'
    assert 'page=1' in obj.cursor['realtime']['next']
    obj = spider(trading, 'doj')
    list(obj.parse_listing(response(url, json.dumps(data(datetime(2024,1,1,tzinfo=UTC))), lane='history')))
    assert obj.cursor['history']['next'] is None


@pytest.mark.parametrize('bad_body', [False, True])
def test_doj_body_identity_date_and_validation(trading, bad_body):
    obj = spider(trading, 'doj')
    obj.run.history_deadline = time.monotonic() - 1
    identifier = str(uuid.uuid4())
    doc = {'url': f'https://www.justice.gov/api/v1/press_releases/{identifier}.json', 'kind': 'doj'}
    obj.cursor['history']['pending'] = [doc]
    body = '<h1>Just a moment...</h1>' if bad_body else '<p>The cryptocurrency exchange pleaded guilty to violations of financial regulations.</p>'
    record = {'uuid': identifier, 'url': 'https://justice.gov/opa/pr/test', 'date': '1735732800', 'title': 'Exchange &amp; settlement', 'body': body, 'changed': '<time datetime="2025-01-02T12:00:00Z">x</time>'}
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps({'metadata': {'responseInfo': {'status': 200}}, 'results': [record]}), lane='history', document=doc))]
    if bad_body:
        assert asyncio.run(collect()) == []
        assert obj.run.blocked and obj.run.errors
        assert obj.cursor['document_retries'][doc['url']]['attempts'] == 1
        assert obj.cursor['history']['pending'] == []
    else:
        item, = asyncio.run(collect())
        assert item['url'] == 'https://www.justice.gov/opa/pr/test'
        assert item['title'] == 'Exchange & settlement'
        assert item['extra']['published_at_precision'] == 'date'
        assert item['source_updated_at'] == datetime(2025,1,2,12,tzinfo=UTC)


def test_doj_old_discovery_cursor_is_rebuilt(trading):
    obj = spider(trading, 'doj')
    obj.cursor.update(checked={'old': 1}, history={'entry': 2}, attachment_retries={})
    obj.first_requests()
    assert obj.cursor['transport'] == 'doj-api-v1'
    assert 'history' not in obj.cursor and obj.cursor['realtime']['entry'] == 0
    assert obj.run.history_target == datetime(2025,1,1,tzinfo=UTC)


def test_bill_undated_text_is_visible_retry_gap(trading):
    obj = spider(trading, 'congress')
    obj.run.history_deadline = time.monotonic() - 1
    base = 'https://api.congress.gov/v3/bill/119/s/1582'
    doc = {'url': base + '/text', 'kind': 'texts', 'title': 'GENIUS Act', 'bill': base}
    finish = {'url': base, 'kind': 'bill_finish'}
    obj.cursor['history'].update(bill={'url': base, 'document_url': base, 'title': 'GENIUS Act', 'version': 'v1', 'reasons': ['crypto_policy'], 'bodies': 1}, pending=[doc, finish])
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps({'textVersions': [{'date': None, 'formats': []}]}), lane='history', document=doc))]
    asyncio.run(collect())
    obj.finish_bill('history')
    assert obj.cursor['bill_retries'][base]['attempts'] == 1
    assert obj.run.stats['unresolved_bills'] == 1
    assert '缺少官方日期' in obj.cursor['bill_retries'][base]['error']


def test_public_api_and_pdf_fixtures_replay_without_io(trading):
    import runpy
    from pathlib import Path
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/smoke_us_news.py'))
    items = asyncio.run(script['public_api_items']())
    assert len(items) == 3
    assert all(item['extra']['content_scope'] == 'article' for item in items)



def test_congress_official_xml_crosscheck(trading):
    import runpy
    from pathlib import Path
    script = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/smoke_us_news.py'))
    assert len(asyncio.run(script['congress_reference_items']())) == 3


def test_congress_meeting_includes_deduplicated_witness_pdfs(trading, monkeypatch):
    from alphapulse.crawler.spiders import us_official
    monkeypatch.setattr(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key='test', crawler_contact_email='test@example.invalid'))
    obj = spider(trading, 'congress')
    doc = {'url': 'https://api.congress.gov/v3/committee-meeting/119/house/1', 'kind': 'meeting'}
    obj.cursor['history']['pending'] = [doc]
    pdf = {'url': 'https://www.congress.gov/witness.pdf', 'format': 'PDF', 'documentType': 'Witness Statement'}
    data = {'committeeMeeting': {'title': 'Digital asset regulation', 'updateDate': '2025-01-01T12:00:00Z', 'meetingDocuments': [pdf], 'witnessDocuments': [pdf]}}
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps(data), document=doc, lane='history'))]
    req, = asyncio.run(collect())
    assert req.url == pdf['url']
    assert len(req.meta['parsed']['extra']['attachments']) == 1
    assert req.meta['parsed']['extra']['attachments'][0]['title'] == 'Witness Statement'
    obj.run.history_deadline = time.monotonic() - 1
    req.meta['raw_response_id'] = 13
    item, = obj.parse_attachment(Response(req.url, status=404, request=req))
    assert item['extra']['content_scope'] == 'partial'
    assert obj.cursor['attachment_retries'][doc['url']]['document'] == doc


def test_dated_but_unavailable_bill_text_remains_a_gap(trading):
    obj = spider(trading, 'congress')
    obj.run.history_deadline = time.monotonic() - 1
    base = 'https://api.congress.gov/v3/bill/119/s/1582'
    doc = {'url': base + '/text', 'kind': 'texts', 'title': 'GENIUS Act', 'bill': base}
    obj.cursor['history'].update(bill={'url': base, 'document_url': base, 'title': 'GENIUS Act', 'version': 'v1', 'reasons': ['crypto_policy'], 'bodies': 1}, pending=[doc, {'url': base, 'kind': 'bill_finish'}])
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps({'textVersions': [{'date': '2025-01-01', 'formats': []}]}), lane='history', document=doc))]
    asyncio.run(collect())
    obj.finish_bill('history')
    assert obj.run.stats['unresolved_bills'] == 1
    assert '缺少支持的正文格式' in obj.cursor['bill_retries'][base]['error']



def test_real_congress_api_fixture_replays_with_visible_source_gap(trading, tmp_path):
    import runpy
    import shutil
    from pathlib import Path
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    original = Path(__file__).resolve().parents[2] / 'scripts'
    for name in ('smoke_us_news.py', 'capture_congress.py'):
        shutil.copyfile(original / name, scripts / name)
    namespace = runpy.run_path(str(scripts / 'smoke_us_news.py'))
    items = asyncio.run(namespace['congress_api_items']())
    assert len(items) == 100
    meetings = [item for item in items if item['extra'].get('official_meeting')]
    assert len(meetings) == 1
    assert meetings[0]['extra']['content_scope'] == 'partial'
    attachments = meetings[0]['extra']['attachments']
    assert len(attachments) == 15
    assert len({a['url'] for a in attachments}) == 15
    assert sum(a['status'] == 'complete' for a in attachments) == 14
    assert sum(a['status'] == 'unavailable' for a in attachments) == 1
    assert any(item['extra']['content_scope'] == 'official_action' for item in items)


def test_terminal_news_gap_survives_rediscovery(trading):
    obj = spider(trading, 'doj')
    url = 'https://www.justice.gov/opa/pr/example'
    doc = {'url': url, 'kind': 'article'}
    obj.cursor['history']['pending'] = [doc]
    obj.run.history_deadline = time.monotonic() - 1
    obj.cursor['attachment_retries'] = {url: {'attempts': 3, 'error': 'unsupported image', 'failed_urls': [url + '.png']}}
    obj.retire_exhausted()
    parsed = {'url': url, 'title': 'Crypto exchange regulation', 'external_id': 'x', 'content_html': '<p>Updated cryptocurrency exchange regulations.</p>',
              'published_at': datetime(2026, 11, 4, tzinfo=UTC), 'raw_response_id': 12, 'retry_document': doc,
              'extra': {'attachments': [{'url': url + '.png', 'title': 'Document', 'status': 'pending'}]}}
    item, = obj.attach_or_finish(parsed, 'history')
    assert 'Updated' in item['content_html']
    assert obj.cursor['attachment_retries'][url]['attempts'] == 3
    assert obj.cursor['attachment_retries'][url]['retired_at']


def test_selected_news_requests_have_one_retry_owner(trading):
    obj = spider(trading, 'doj')
    req = obj.request_for(SOURCES['doj'].entries[0], obj.parse_listing, 'realtime')
    assert req.meta['dont_retry'] is True


def test_doj_captioned_photo_is_not_a_document_gap(trading):
    obj = spider(trading, 'doj')
    obj.run.history_deadline = time.monotonic() - 1
    identifier = str(uuid.uuid4())
    doc = {'url': f'https://www.justice.gov/api/v1/press_releases/{identifier}.json', 'kind': 'doj'}
    obj.cursor['history']['pending'] = [doc]
    record = {'uuid': identifier, 'url': 'https://www.justice.gov/opa/pr/test', 'date': '1735732800',
              'title': 'Cryptocurrency exchange settlement', 'body': '<p>The cryptocurrency exchange pleaded guilty to violations of financial regulations.</p>',
              'image': '<a href="/usao-sd/media/1470086">award.png</a>',
              'attachment': [{'file': {'id': '1470086'}, 'alt': 'Photo- Left to right: award recipients.'}]}
    async def collect():
        return [v async for v in obj.parse_document(response(doc['url'], json.dumps({'metadata': {'responseInfo': {'status': 200}}, 'results': [record]}), lane='history', document=doc))]
    item, = asyncio.run(collect())
    assert item['extra']['attachments'] == []
    assert not obj.cursor.get('attachment_retries')


def test_sec_uses_official_dateline_not_last_reviewed_date():
    body = '''<main><h1>SEC regulatory announcement</h1>
    <div class="field--name-dynamic-twig-fieldnode-press-release-lead-in">Washington D.C., Sept. 30, 2026 —</div>
    <div class="node-details-layout__main-region__content"><div class="field--name-body">The Commission proposes digital asset market rules for regulated financial institutions.</div></div>
    <div class="date-modified">Last Reviewed or Updated: November 5, 2026</div></main>'''
    item = article('sec', body, 'https://www.sec.gov/newsroom/press-releases/example')
    assert item['published_at'] == datetime(2026, 9, 30, tzinfo=UTC)
    assert item['extra']['published_at_precision'] == 'date'


def test_attachment_http_failure_spends_durable_retry_budget(trading):
    obj = spider(trading, 'doj')
    obj.run.history_deadline = time.monotonic() - 1
    url = 'https://www.justice.gov/opa/pr/example'
    doc = {'url': url, 'kind': 'doj'}
    obj.cursor['history']['pending'] = [doc]
    parsed = {'url': url, 'title': 'Crypto exchange regulation', 'external_id': 'x', 'content_html': '<p>Cryptocurrency exchange regulations.</p>',
              'published_at': datetime(2026, 11, 4, tzinfo=UTC), 'raw_response_id': 12, 'retry_document': doc,
              'extra': {'attachments': [{'url': 'https://www.justice.gov/media/1/dl', 'title': 'Document', 'status': 'pending'}]}}
    request = Request('https://www.justice.gov/media/1/dl', meta={'lane': 'history', 'parsed': parsed})
    failure = Failure(HttpError(Response(request.url, status=503, request=request)))
    failure.request = request
    item, = obj.on_request_error(failure)
    assert item['extra']['content_scope'] == 'partial'
    assert obj.cursor['attachment_retries'][url]['attempts'] == 1
    assert '503' in obj.cursor['attachment_retries'][url]['error']


@pytest.mark.parametrize('dated', [True, False])
def test_sec_historical_listing_stops_only_with_verified_dates(trading, dated, monkeypatch):
    monkeypatch.setattr('alphapulse.crawler.spiders.us_official.configuration_error', lambda code: None)
    obj = spider(trading, 'sec')
    stamp = '<time datetime="2024-12-30T12:00:00Z">Dec 30</time>' if dated else ''
    body = '<h1>Press releases</h1><table><tr><td>' + stamp + '</td><td><a href="/newsroom/press-releases/2024-199">Release</a></td></tr></table><a rel="next" href="?page=1">Next</a>'
    requests = list(obj.parse_listing(response(SOURCES['sec'].entries[0], body, lane='history')))
    if dated:
        assert all('/2024-199' not in r.url and '?page=1' not in r.url for r in requests)
    else:
        assert any('/2024-199' in r.url for r in requests)
        assert obj.cursor['history']['next'].endswith('?page=1')
