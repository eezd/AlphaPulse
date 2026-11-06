"""Validate source migration and real public-document idempotency in an isolated database."""
import asyncio
import copy
import gzip
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.us_news import SOURCES, article, identity
from alphapulse.crawler.spiders.base import RunState, html_to_text
from alphapulse.crawler.spiders.us_official import USOfficialSpider
from alphapulse.crawler import pipelines
from alphapulse.models import CrawlRun, NewsItem, NewsRevision, RawBody, RawResponse, Source

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(pipelines.__file__).resolve().parents[3]


async def public_api_items():
    """Replay captured public API text bodies without network or database I/O."""
    import time
    import uuid
    from datetime import UTC, datetime
    from scrapy import Request
    from scrapy.http import HtmlResponse
    from alphapulse.crawler.spiders.base import RunState
    samples = json.load(gzip.open(BACKEND_ROOT / 'tests/fixtures/us_news/api_samples.json.gz', 'rt'))
    items = []
    for sample in (s for s in samples if s['kind'] != 'pdf'):
        obj = USOfficialSpider(name=sample['source'])
        now = datetime.now(UTC)
        obj.run = RunState(uuid.uuid4(), 'schedule', now, now, history_target=datetime(2025, 1, 1, tzinfo=UTC), history_deadline=time.monotonic()-1)
        doc = {k: sample[k] for k in ('url', 'kind')}
        if doc['kind'] == 'register_issue':
            doc.update(day='2025-07-18', count=106)
        obj.cursor = {'history': {'entry': 0, 'pending': [doc], 'day': '2025-07-18'}}
        req = Request(doc['url'], meta={'lane': 'history', 'document': doc, 'raw_response_id': None})
        output = [v async for v in obj.parse_document(HtmlResponse(req.url, body=sample['body'].encode(), encoding='utf-8', request=req))]
        if doc['kind'] == 'register_issue':
            assert len(obj.cursor['history']['pending']) == 106
            continue
        if sample.get('excluded'):
            assert not output
            continue
        assert len(output) == 1 and output[0]['extra']['content_scope'] == 'article'
        assert all(a['status'] == 'skipped_policy' for a in output[0]['extra']['attachments'])
        items += output
    assert len(items) == 3
    return items


async def congress_reference_items():
    """Cross-check public GovInfo XML through the text adapter, not the API adapter."""
    import time
    import uuid
    from datetime import UTC, datetime
    from lxml import etree
    from scrapy import Request
    from scrapy.http import HtmlResponse
    samples = json.load(gzip.open(BACKEND_ROOT / 'tests/fixtures/us_news/congress_billstatus.json.gz', 'rt'))
    items = []
    assert len(samples) == 3
    for sample in samples:
        root = etree.fromstring(sample['body'].encode(), etree.XMLParser(resolve_entities=False, no_network=True))
        versions = root.findall('.//textVersions/item')
        if sample['bill'] == 's1582':
            assert any(v.findtext('type') == 'Enrolled Bill' and not v.findtext('date') for v in versions)
        obj = USOfficialSpider(name='congress')
        now = datetime.now(UTC)
        obj.run = RunState(uuid.uuid4(), 'schedule', now, now, history_target=datetime(2025,1,1,tzinfo=UTC), history_deadline=time.monotonic()-1)
        text = sample['text']
        doc = {'url': text['url'], 'kind': 'text', 'title': sample['title'], 'at': text['date']}
        obj.cursor = {'history': {'entry': 0, 'pending': [doc]}}
        req = Request(doc['url'], meta={'document': doc, 'lane': 'history', 'raw_response_id': None})
        output = [v async for v in obj.parse_document(HtmlResponse(req.url, body=text['body'].encode(), encoding='utf-8', request=req))]
        assert len(output) == 1 and output[0]['extra']['selection_reasons']
        items += output
    return items


async def congress_api_items():
    """Replay captured Congress API evidence with zero network requests."""
    namespace = runpy.run_path(str(ROOT / 'scripts/capture_congress.py'))
    cache = BACKEND_ROOT / 'tests/fixtures/us_news/congress_api.json.gz'
    capture = namespace['Capture'](cache, 'offline-test-only', limit=0)
    items = []
    try:
        report = await namespace['run'](capture, item_sink=items, meeting_url='https://api.congress.gov/v3/committee-meeting/119/house/119541')
    finally:
        capture.client.close()
    assert capture.requests == 0
    assert len(report['cases']) == 4
    assert all(case.get('parser_accepted') for case in report['cases'])
    assert report['cases'][0]['unresolved_bills'] == 1
    assert report['accepted'] is False  # Undated GENIUS text remains a genuine gap.
    assert len(items) == sum(case['items'] for case in report['cases'])
    return items


async def main():
    url = urlsplit(os.environ['DATABASE_URL'])
    assert url.username == 'ci' and url.password == 'ci-only' and url.path == '/ci'
    assert url.hostname == '127.0.0.1'
    tables = [model.__table__ for model in (Source, CrawlRun, RawBody, RawResponse, NewsItem, NewsRevision)]
    async with engine.begin() as connection:
        await connection.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        await connection.run_sync(lambda conn: Source.metadata.create_all(conn, tables=tables))
        for table, column in [('sources', 'history_cursor'), ('crawl_runs', 'stats'), ('news_revisions', 'extra')]:
            await connection.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
        namespace = runpy.run_path(str(BACKEND_ROOT / 'alembic/versions/20261104_da1234567890_us_official_news.py'))
        def migrate(conn):
            with Operations.context(MigrationContext.configure(conn)):
                namespace['upgrade']()
        await connection.run_sync(migrate)
    async with SessionLocal() as session:
        sources = list(await session.scalars(select(Source)))
        assert len(sources) == 10 and all(not s.enabled for s in sources)
        session.add(Source(code='fed', name='Fed', list_url=SOURCES['fed'].entries[0], enabled=False, interval_seconds=60))
        await session.commit()
    samples = json.load(gzip.open(BACKEND_ROOT / 'tests/fixtures/us_news/public_samples.json.gz', 'rt'))
    assert len(samples) == len({s['url'] for s in samples}) == 50
    spider = SimpleNamespace(run=SimpleNamespace(items_seen=0, items_new=0, items_revised=0))
    pipeline = pipelines.NewsPipeline(SimpleNamespace(spider=spider))
    async def ingest(sample):
        data = article(sample['source'], sample['body'], sample['url'])
        return await pipeline.process_item(dict(source_code=sample['source'], external_id=identity(sample['url']), url=sample['url'], title=data['title'], summary='', content_html=data['content_html'], content_text=html_to_text(data['content_html']), author=None, tags=[], is_paywalled=False, published_at=data['published_at'], source_updated_at=None, extra=data['extra'], raw_response_id=None))
    with patch.object(pipelines, 'redis', SimpleNamespace(publish=AsyncMock())):
        for sample in samples:
            await ingest(sample)
        for sample in samples:
            await ingest(sample)
        assert spider.run.items_new == 50 and spider.run.items_revised == 0
        sample = samples[0]
        changed = {**sample, 'body': sample['body'].replace('</h1>', ' Updated</h1>', 1)}
        await ingest(changed)
        assert spider.run.items_revised == 1
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(NewsItem)) == 50
        assert await session.scalar(select(func.count()).select_from(NewsRevision)) == 51
        assert await session.scalar(select(func.count()).select_from(NewsRevision).where(NewsRevision.extra.is_not(None))) == 51
    # Preserve the legacy RSS identity while enriching from the same public URL.
    from datetime import UTC, datetime, timedelta
    import time
    import uuid
    from scrapy import Request
    from scrapy.http import HtmlResponse
    sample = next(s for s in samples if s['source'] == 'fed')
    async with SessionLocal() as session:
        news = await session.scalar(select(NewsItem).where(NewsItem.url == sample['url']))
        news.external_id = 'legacy-rss-guid'
        baseline = (news.content_hash, news.revision_no)
        await session.commit()
    obj = USOfficialSpider(name='fed')
    now = datetime.now(UTC)
    obj.run = RunState(uuid.uuid4(), 'schedule', now, now-timedelta(hours=72), history_target=datetime(2025, 1, 1, tzinfo=UTC), history_position=now, history_deadline=time.monotonic()-1)
    obj.cursor = {'history': {'entry': 0, 'pending': []}}
    captured = []
    obj.attach_or_finish = lambda parsed, lane: captured.append(parsed) or []
    doc = {'url': sample['url'], 'kind': 'article'}
    request = Request(sample['url'], meta={'document': doc, 'lane': 'history', 'raw_response_id': None})
    valid = HtmlResponse(sample['url'], body=sample['body'].encode(), encoding='utf-8', request=request)
    assert not [v async for v in obj.parse_document(valid)]
    assert captured[0]['external_id'] == 'legacy-rss-guid'
    captured.clear()
    invalid = HtmlResponse(sample['url'], body=b'<h1>Just a moment...</h1><p>Enable JavaScript and cookies to continue.</p>', encoding='utf-8', request=request)
    obj.cursor['history']['pending'] = [doc]
    assert not [item async for item in obj.parse_document(invalid)]
    assert obj.run.errors and obj.run.blocked
    assert obj.cursor['document_retries'][doc['url']]['attempts'] == 1
    assert not captured
    async with SessionLocal() as session:
        news = await session.scalar(select(NewsItem).where(NewsItem.external_id == 'legacy-rss-guid'))
        assert (news.content_hash, news.revision_no) == baseline
        assert await session.scalar(select(func.count()).select_from(NewsRevision)) == 51
    # A failed checkpoint commit must recover before a new round can read it.
    async with SessionLocal() as session:
        source = await session.get(Source, 'ofac')
        source.enabled = True
        await session.commit()
    obj = USOfficialSpider(name='ofac')
    obj.run = RunState(uuid.uuid4(), 'schedule', now, now-timedelta(hours=72), official_cursor={'checkpoint_probe': 'committed'})
    obj.crawler = SimpleNamespace(engine=SimpleNamespace(crawl=lambda request: None))
    finish = obj._finish_run
    attempts = []
    async def transient_failure(run):
        attempts.append(run.id)
        if len(attempts) == 1:
            raise RuntimeError('isolated checkpoint failure')
        await finish(run)
    obj._finish_run = transient_failure
    original_id = obj.run.id
    obj._close_run()
    await asyncio.gather(obj.finish_task, return_exceptions=True)
    await obj.start_round('schedule')
    await asyncio.gather(*obj._tasks)
    assert attempts == [original_id, original_id] and obj.run is not None
    assert obj.cursor['checkpoint_probe'] == 'committed'
    async with SessionLocal() as session:
        saved = await session.get(CrawlRun, original_id)
        assert saved.status == 'success'
        assert (await session.get(Source, 'ofac')).history_cursor['checkpoint_probe'] == 'committed'
    # A failed document preserves safe reads and explicit gaps, not lane positions.
    async with SessionLocal() as session:
        before_cursor = copy.deepcopy((await session.get(Source, 'ofac')).history_cursor)
    completed = {'https://ofac.treasury.gov/recent-actions/20261104': obj.run.started_at.timestamp()}
    gap_url = 'https://ofac.treasury.gov/recent-actions/20261103'
    gaps = {gap_url: {'document': {'url': gap_url, 'kind': 'article'}, 'attempts': 1, 'next_at': obj.run.started_at.timestamp()+3600, 'error': 'HTTP503'}}
    failed_run = copy.deepcopy(obj.run)
    failed_run.id = uuid.uuid4()
    failed_run.errors = ['document unavailable']
    failed_run.completed_documents_safe = True
    failed_run.official_cursor = {'checked': completed, 'document_retries': gaps, 'realtime': {'entry': 99}, 'history': {'entry': 99}}
    await obj._finish_run(failed_run)
    async with SessionLocal() as session:
        cursor = (await session.get(Source, 'ofac')).history_cursor
        assert cursor['checked'] == completed and cursor['document_retries'] == gaps
        assert cursor.get('history') == before_cursor.get('history')
        assert cursor.get('realtime') == before_cursor.get('realtime')
        assert (await session.get(CrawlRun, failed_run.id)).status == 'failed'
    unsafe_run = copy.deepcopy(failed_run)
    unsafe_run.id = uuid.uuid4()
    unsafe_run.completed_documents_safe = False
    unsafe_run.official_cursor['checked'] = {'must-not-be-skipped': 1}
    await obj._finish_run(unsafe_run)
    async with SessionLocal() as session:
        assert (await session.get(Source, 'ofac')).history_cursor['checked'] == completed
    # API text packages use the real parser and database revision pipeline too.
    api_items = await public_api_items() + await congress_reference_items() + await congress_api_items()
    with patch.object(pipelines, 'redis', SimpleNamespace(publish=AsyncMock())):
        for item in api_items:
            await pipeline.process_item(dict(item))
        async with SessionLocal() as session:
            baseline_count = await session.scalar(select(func.count()).select_from(NewsRevision))
        for item in api_items:
            await pipeline.process_item(dict(item))
        async with SessionLocal() as session:
            assert await session.scalar(select(func.count()).select_from(NewsItem)) == 50 + len(api_items)
            assert await session.scalar(select(func.count()).select_from(NewsRevision)) == baseline_count
    revised = copy.deepcopy(api_items[0])
    revised['title'] += ' Updated'
    with patch.object(pipelines, 'redis', SimpleNamespace(publish=AsyncMock())):
        await pipeline.process_item(dict(revised))
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(NewsRevision)) == baseline_count + 1
    await engine.dispose()
    print('US source migration, HTML, API text, GovInfo and Congress packages, API text replay and revision, negative screening, daily XML, RSS identity, challenge rejection, checkpoint recovery and failed-document persistence passed')


if __name__ == '__main__':
    asyncio.run(main())
