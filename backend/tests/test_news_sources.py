import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from scrapy.http import Request, XmlResponse
from sqlalchemy import create_engine, select, text


def make_spider(spider_type):
    from alphapulse.crawler.spiders.base import RunState
    spider = spider_type()
    now = datetime(2026, 10, 21, tzinfo=UTC)
    spider.run = RunState(uuid.uuid4(), "schedule", now, now - timedelta(hours=72))
    return spider


def feed_response(items, url="https://example.invalid/feed.xml"):
    return XmlResponse(
        url=url,
        body=f'''<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>{items}</channel></rss>'''.encode(),
        encoding="utf-8", request=Request(url, meta={"raw_response_id": 12}),
    )


ITEM = """<item><title>Bitcoin &amp; Ethereum</title>
<link>https://example.invalid/news/bitcoin</link><guid>story-1</guid>
<pubDate>Tue, 20 Oct 2026 08:30:00 EDT</pubDate>
<description><![CDATA[<p>公开摘要 &amp; 数据</p>]]></description>
<dc:creator>News desk</dc:creator><category>Markets</category></item>"""


@pytest.mark.parametrize("source", ["CoinDeskSpider", "FedSpider", "BeaSpider"])
def test_rss_preserves_original_summary_time_and_provenance(trading, source):
    from alphapulse.crawler.spiders import rss
    spider = make_spider(getattr(rss, source))
    requests = list(spider.first_requests())
    assert [request.url for request in requests] == list(spider.feeds)
    assert all(request.meta["run_id"] == spider.run.id for request in requests)
    item, = spider.parse_feed(feed_response(ITEM))
    assert item["source_code"] == spider.name
    assert item["title"] == "Bitcoin & Ethereum"
    assert item["summary"] == item["content_text"] == "公开摘要 & 数据"
    assert item["content_html"] == "<p>公开摘要 &amp; 数据</p>"
    assert item["author"] == "News desk" and item["tags"] == ["Markets"]
    assert item["published_at"] == datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
    assert item["source_updated_at"] is None
    assert item["extra"] == {"language": "en", "content_scope": "feed_summary", "feed_url": "https://example.invalid/feed.xml"}
    assert item["raw_response_id"] == 12


def test_rss_identity_survives_revisions_and_different_subfeeds(trading):
    from alphapulse.crawler.spiders.rss import FedSpider
    spider = make_spider(FedSpider)
    original, = spider.parse_feed(feed_response(ITEM))
    revised, = spider.parse_feed(feed_response(ITEM.replace("Bitcoin &amp; Ethereum", "Revised headline"), "https://example.invalid/other.xml"))
    assert original["external_id"] == revised["external_id"]
    long_url = "https://example.invalid/" + "article" * 30
    no_guid = ITEM.replace("<guid>story-1</guid>", "<guid> </guid>").replace("https://example.invalid/news/bitcoin", long_url)
    first, = spider.parse_feed(feed_response(no_guid))
    again, = spider.parse_feed(feed_response(no_guid))
    assert len(first["external_id"]) == 64 and first["external_id"] == again["external_id"]
    assert first["url"] == long_url


@pytest.mark.parametrize("bad_item", [
    ITEM.replace("Tue, 20 Oct 2026 08:30:00 EDT", "invalid date"),
    ITEM.replace(" EDT", ""),
    ITEM.replace("<title>Bitcoin &amp; Ethereum</title>", ""),
    ITEM.replace("https://example.invalid/news/bitcoin", "javascript:alert(1)"),
    ITEM.replace("<pubDate>Tue, 20 Oct 2026 08:30:00 EDT</pubDate>", ""),
])
def test_rss_isolates_bad_entries_and_records_visible_failure(trading, bad_item):
    from alphapulse.crawler.spiders.rss import CoinDeskSpider
    spider = make_spider(CoinDeskSpider)
    items = list(spider.parse_feed(feed_response(bad_item + ITEM)))
    assert len(items) == 1 and items[0]["title"] == "Bitcoin & Ethereum"
    assert len(spider.run.errors) == 1 and "RSS 条目解析失败" in spider.run.errors[0]


def test_rss_rejects_html_instead_of_reporting_empty_success(trading):
    from alphapulse.crawler.spiders.rss import CoinDeskSpider
    response = XmlResponse(url="https://example.invalid/rss", body=b"<html><body>Access denied</body></html>")
    with pytest.raises(ValueError, match="不是 RSS/Atom"):
        list(make_spider(CoinDeskSpider).parse_feed(response))


@pytest.mark.parametrize("query,sources,expected", [
    ("比特币", None, [1, 2, 16]),
    ("BITCOIN", None, [1, 2, 16]),
    ("BTC", ["coindesk"], [2, 16]),
    ("以太坊", None, [3, 15]),
    ("ETH", None, [3, 15]),
    ("ether", None, [3, 15]),
    ("美联储", None, [4, 5, 17]),
    ("FOMC", None, [4, 5, 17]),
    ("Federal Open Market Committee", ["fed"], [5, 17]),
    ("CPI", None, [6, 7]),
    ("非农", None, [8]),
    ("GDP", None, [9]),
    ("PCE", None, [10]),
    ("50%_profit", None, [11]),
    ("比特币 ETF", None, []),
])
def test_news_search_matches_topics_across_languages_and_keeps_literal_queries(trading, query, sources, expected):
    from alphapulse.api.news import news_filters
    from alphapulse.models import NewsItem
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE news_items (id INTEGER, source_code TEXT, title TEXT, content_text TEXT)"))
            connection.execute(text("INSERT INTO news_items VALUES (:id, :source, :title, :body)"), [
                {"id": i, "source": source, "title": title, "body": body}
                for i, source, title, body in [
                    (1, "cls_telegraph", "比特币消息", ""), (2, "coindesk", "Bitcoin market update", ""),
                    (3, "coindesk", "Network update", "Ethereum update"),
                    (4, "wscn_live", "美联储声明", ""), (5, "fed", "Federal Reserve statement", ""),
                    (6, "bls", "Consumer Price Index", ""), (7, "cls_telegraph", "消费者价格指数公布", ""),
                    (8, "bls", "Employment Situation", ""), (9, "bea", "Gross Domestic Product", ""),
                    (10, "bea", "Personal Income and Outlays", "Personal consumption expenditures"),
                    (11, "coindesk", "50%_profit", ""), (12, "coindesk", "50xxx_profit", ""),
                    (13, "coindesk", "New method of employment research", ""), (14, "coindesk", "Ethernet hardware", ""),
                    (15, "coindesk", "ETH update", ""), (16, "coindesk", "BTC news", ""),
                    (17, "fed", "Minutes of the Federal Open Market Committee, September 15-16, 2026", ""),
                ]
            ])
            statement = select(NewsItem.id).where(*news_filters(sources, query, None, None)).order_by(NewsItem.id)
            assert connection.execute(statement).scalars().all() == expected
    finally:
        engine.dispose()


@pytest.mark.parametrize("headline,matched", [
    ("Bitcoin ETF inflows", True), ("ethereum network update", True),
    ("ETH rally", True), ("CRYPTO regulation", True), ("Stablecoins update", True),
    ("Binance announcement", True), ("比特币消息", True), ("微策略增持", True),
    ("New method for employment statistics", False), ("Ethernet hardware demand", False),
    ("A hypothetical company", False),
])
def test_english_crypto_filter_uses_whole_words(trading, headline, matched):
    from alphapulse.trading.news_signals import CRYPTO_PATTERN
    assert bool(re.search(CRYPTO_PATTERN, headline, re.IGNORECASE)) is matched


def test_macro_label_receives_summary_separately_from_truncated_title(trading, monkeypatch):
    from alphapulse.ai import llm
    from alphapulse.models import TradingAccount
    from alphapulse.trading import news_signals

    title = "GDP, Industries, Corporate Profits, State GDP, and State Personal Income; " * 4
    summary = "Real gross domestic product increased at an annual rate of 2.2 percent."
    provider = SimpleNamespace(model="test-only")
    writes = []

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, statement):
            if statement.is_select:
                return SimpleNamespace(all=lambda: [(123, title, summary)])
            writes.append(statement.compile().params)
        async def get(self, model, key):
            return SimpleNamespace(provider_id=1) if model is TradingAccount else provider
        def expunge(self, value):
            assert value is provider
        async def commit(self):
            pass

    async def stream(actual_provider, system, messages, tools, effort):
        assert actual_provider is provider
        item, = json.loads(messages[0]["text"])
        assert item["id"] == 123 and item["summary"] == summary
        assert len(item["title"]) == 200
        assert "neutral" in system and "市场预期" in system
        yield llm.Completion(tool_calls=[{"name": "label", "arguments": {
            "items": [{"id": 123, "category": "event", "direction": "neutral"}],
        }}])

    monkeypatch.setattr(news_signals, "SessionLocal", Session)
    monkeypatch.setattr(llm, "stream", stream)
    assert asyncio.run(news_signals.label_new()) == 1
    assert writes[0]["news_id_m0"] == 123 and writes[0]["direction_m0"] == "neutral"


def test_news_tools_accept_foreign_sources_and_disclose_summary_scope(trading, monkeypatch):
    from alphapulse.ai import tools
    args = tools.SearchNews(query="CPI", sources=["coindesk", "fed", "bls", "bea"])
    assert len(args.sources) == 4
    news = SimpleNamespace(
        id=1, source_code="fed", published_at=datetime(2026, 10, 21, tzinfo=UTC),
        title="Policy statement", url="https://example.invalid/statement", is_paywalled=False,
        content_text="Public feed summary", extra={"content_scope": "feed_summary"},
    )
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, model, news_id):
            return news
    monkeypatch.setattr(tools, "SessionLocal", Session)
    result = json.loads(asyncio.run(tools.get_news(tools.GetNews(id=1))).result)
    assert result["source"] == "美联储·政策与讲话"
    assert result["content_scope"] == "feed_summary" and result["content"] == news.content_text


@pytest.mark.parametrize("bad_date", [None, "2026-10-20T08:30:00", "invalid date", ""])
def test_atom_uses_bls_published_time_and_isolates_invalid_dates(trading, bad_date):
    from alphapulse.crawler.spiders.rss import BlsSpider
    published = bad_date if bad_date is not None else "2026-10-20T08:30:00.2-04:00"
    entry = f"""<entry><id>cpi-release</id><title>Consumer Price Index</title>
    <link href="https://www.bls.gov/news.release/archives/cpi_10202026.htm"/><published>{published}</published>
    <updated>2026-10-20T09:00:00-04:00</updated>
    <content>Monthly change &amp; value &lt; 1%</content><category>News Release</category></entry>"""
    url = "https://example.invalid/cpi.rss"
    response = XmlResponse(url=url, body=f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'.encode(),
                           request=Request(url, meta={"raw_response_id": 13}))
    spider = make_spider(BlsSpider)
    items = list(spider.parse_feed(response))
    if bad_date is not None:
        assert not items and len(spider.run.errors) == 1
    else:
        item, = items
        assert item["published_at"] == datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
        assert item["extra"]["feed_published_at"] == "2026-10-20T12:30:00.200000+00:00"
        assert item["source_updated_at"] == datetime(2026, 10, 20, 13, tzinfo=UTC)
        assert item["summary"] == item["content_text"] == "Monthly change & value < 1%"
        assert "&lt;" in item["content_html"] and item["tags"] == ["News Release"]
        assert item["extra"]["content_scope"] == "feed_summary" and not spider.run.errors


@pytest.mark.parametrize("program,date,feed_time,expected", [
    ("cpi", "10142026", "2026-10-14T07:50:31.2-04:00", datetime(2026, 10, 14, 12, 30, tzinfo=UTC)),
    ("ppi", "10152026", "2026-10-15T07:50:51.146-04:00", datetime(2026, 10, 15, 12, 30, tzinfo=UTC)),
    ("empsit", "10022026", "2026-10-02T07:51:08.289-04:00", datetime(2026, 10, 2, 12, 30, tzinfo=UTC)),
    ("cpi", "01132026", "2026-01-13T07:50:31-05:00", datetime(2026, 1, 13, 13, 30, tzinfo=UTC)),
])
def test_bls_uses_official_release_time_and_keeps_feed_timestamp(trading, program, date, feed_time, expected):
    from alphapulse.crawler.spiders.rss import BlsSpider
    from alphapulse.trading.news_signals import is_forward_sample

    url = f"https://www.bls.gov/feed/{program}.rss"
    response = XmlResponse(url=url, body=f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>{program}-{date}</id><title>Official release</title>
        <link href="https://www.bls.gov/news.release/archives/{program}_{date}.htm"/>
        <published>{feed_time}</published><content>Public summary</content>
        </entry></feed>'''.encode(), request=Request(url, meta={"raw_response_id": 13}))
    spider = make_spider(BlsSpider)
    item, = spider.parse_feed(response)
    assert item["published_at"] == expected and not spider.run.errors
    assert datetime.fromisoformat(item["extra"]["feed_published_at"]) == datetime.fromisoformat(feed_time)
    assert item["extra"]["published_at_basis"] == "bls_release_schedule"
    assert is_forward_sample(item["published_at"], expected + timedelta(minutes=1))
    assert not is_forward_sample(item["published_at"], expected + timedelta(minutes=6))


def test_bls_rejects_unrecognized_release_date_instead_of_guessing(trading):
    from alphapulse.crawler.spiders.rss import BlsSpider

    spider = make_spider(BlsSpider)
    assert not list(spider.parse_feed(feed_response(ITEM)))
    assert len(spider.run.errors) == 1 and "公告日期" in spider.run.errors[0]


def test_bea_repairs_only_its_observed_official_schemeless_link(trading):
    from alphapulse.crawler.spiders.rss import BeaSpider, CoinDeskSpider
    response = feed_response(ITEM.replace("https://example.invalid/news/bitcoin", "www.bea.gov/news/2026/gdp"))
    item, = make_spider(BeaSpider).parse_feed(response)
    assert item["url"] == "https://www.bea.gov/news/2026/gdp"
    other = make_spider(CoinDeskSpider)
    assert not list(other.parse_feed(response)) and len(other.run.errors) == 1


@pytest.mark.parametrize("status", [200, 302, 403])
def test_bls_downloader_preserves_compressed_body_status_and_redirect_for_scrapy(trading, monkeypatch, status):
    import gzip
    import ssl

    import httpx

    from alphapulse.crawler.spiders import rss
    original_client = httpx.AsyncClient
    body = gzip.compress(b"<feed>Public announcement</feed>")
    def handle(request):
        assert request.method == "GET"
        assert request.headers["accept-language"] == "en-US"
        return httpx.Response(status, headers={"Content-Encoding": "gzip", "Content-Type": "application/atom+xml",
                                               "Location": "https://www.bls.gov/feed/cpi.rss"}, stream=httpx.ByteStream(body))
    def client(**kwargs):
        context = kwargs["verify"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2 and context.security_level >= 2
        return original_client(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(rss.httpx, "AsyncClient", client)
    request = Request("https://www.bls.gov/feed/cpi.rss", headers={"Accept-Language": "en-US"})
    response = asyncio.run(rss.BlsDownloadHandler(15).download_request(request))
    assert response.status == status and response.body == body
    assert response.headers["Content-Encoding"] == b"gzip"
    assert response.headers["Location"] == b"https://www.bls.gov/feed/cpi.rss"
    assert response.request is request


def test_bls_transport_error_reaches_existing_scrapy_retry_boundary(trading, monkeypatch):
    import httpx

    from alphapulse.crawler.spiders import rss
    original_client = httpx.AsyncClient
    def handle(request):
        raise httpx.ReadTimeout("transient timeout", request=request)
    monkeypatch.setattr(rss.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(httpx.ReadTimeout, match="transient timeout") as exc:
        asyncio.run(rss.BlsDownloadHandler(15).download_request(Request("https://www.bls.gov/feed/cpi.rss")))
    assert isinstance(exc.value, tuple(x for x in rss.BlsSpider.custom_settings["RETRY_EXCEPTIONS"] if isinstance(x, type)))


@pytest.mark.parametrize('kind', ['summary', 'content'])
def test_custom_atom_uses_article_link_and_preserves_html_and_scope(trading, kind):
    from alphapulse.crawler.spiders.rss import CustomRssSpider
    from alphapulse.crawler.spiders.base import RunState
    spider = CustomRssSpider(source_code='rss_'+'a'*24)
    now = datetime.now(UTC)
    spider.run = RunState(uuid.uuid4(), 'schedule', now, now-timedelta(hours=72))
    response = XmlResponse(url='https://example.invalid/feed.xml', body=f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Public statement</title><id>statement-1</id><published>2026-10-31T00:00:00Z</published>
      <link rel="self" href="https://example.invalid/entry.xml"/><link rel="alternate" href="https://example.invalid/article"/>
      <{kind} type="html">&lt;p&gt;Public &amp;amp; original&lt;/p&gt;</{kind}><category term="Politics"/>
      </entry></feed>'''.encode(), request=Request('https://example.invalid/feed.xml', meta={'raw_response_id': 12}))
    item, = spider.parse_feed(response)
    assert item['url'] == 'https://example.invalid/article'
    assert item['content_text'] == 'Public & original'
    assert item['tags'] == ['Politics']
    assert item['extra']['content_scope'] == ('feed_content' if kind == 'content' else 'feed_summary')


def test_new_rss_is_paused_and_discovered_without_manager_restart(trading, monkeypatch):
    from unittest.mock import AsyncMock
    from alphapulse.api.crawler import RssIn, create_rss
    from alphapulse.crawler import manager
    from alphapulse.news_risk import research
    added = []
    session = SimpleNamespace(add=added.append, commit=AsyncMock())
    monkeypatch.setattr(research, 'public_address', AsyncMock())
    async def check():
        await create_rss(RssIn(name='Global news', url='https://example.invalid/feed.xml'), session)
        source, = added
        assert source.adapter == 'rss' and source.enabled is False
        source.paused_until = None
        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.scalars.return_value = SimpleNamespace(all=lambda: [source])
        monkeypatch.setattr(manager, 'SessionLocal', lambda: fake_session)
        instance = manager.Manager()
        supervised = []
        async def supervise(worker): supervised.append(worker.code)
        instance.supervise = supervise
        async with asyncio.TaskGroup() as group:
            instance.supervisor_group = group
            await instance.schedule_once(set())
            await asyncio.sleep(0)
            await instance.schedule_once(set())
        assert supervised == [source.code]
        assert instance.workers[source.code].adapter == 'rss'
    asyncio.run(check())


@pytest.mark.parametrize('encoding,title', [('iso-8859-1', 'Café résumé'), ('utf-8', '中文说明 Café')])
@pytest.mark.parametrize('compressed', [False, True])
def test_custom_feed_preserves_declared_xml_encoding_through_public_downloader(trading, monkeypatch, encoding, title, compressed):
    import gzip
    from urllib.parse import urlsplit
    from unittest.mock import AsyncMock
    import httpx
    from alphapulse.crawler.spiders import rss
    from alphapulse.news_risk import research
    url = 'https://example.invalid/feed.xml'
    item_xml = ITEM.replace('Bitcoin &amp; Ethereum', title).replace('公开摘要 &amp; 数据', 'Public summary')
    body = f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>{item_xml}</channel></rss>'.encode(encoding)
    original_client = httpx.AsyncClient
    def handle(request):
        assert request.url.host == '8.8.8.8' and request.headers['host'] == 'example.invalid'
        headers = {'content-type': f'application/rss+xml; charset={encoding}'}
        if compressed: headers['content-encoding'] = 'gzip'
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(gzip.compress(body) if compressed else body))
    monkeypatch.setattr(research, 'public_address', AsyncMock(return_value=(urlsplit(url), '8.8.8.8')))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    response = asyncio.run(rss.PublicFeedDownloadHandler().download_request(Request(url, meta={'raw_response_id': 12})))
    assert response.body == body and 'content-encoding' not in response.headers
    assert response.headers['content-type'].decode() == f'application/rss+xml; charset={encoding}'
    spider = make_spider(rss.CoinDeskSpider)
    item, = spider.parse_feed(response)
    assert item['title'] == title and not spider.run.errors


def test_custom_feed_keeps_http_failure_status_for_scrapy_retry(trading, monkeypatch):
    from urllib.parse import urlsplit
    from unittest.mock import AsyncMock
    import httpx
    from alphapulse.crawler.spiders import rss
    from alphapulse.news_risk import research
    url = 'https://example.invalid/feed.xml'
    original_client = httpx.AsyncClient
    monkeypatch.setattr(research, 'public_address', AsyncMock(return_value=(urlsplit(url), '8.8.8.8')))
    transport = httpx.MockTransport(lambda request: httpx.Response(503, content=b'Service unavailable'))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=transport, **kwargs))
    response = asyncio.run(rss.PublicFeedDownloadHandler().download_request(Request(url)))
    assert response.status == 503 and response.body == b'Service unavailable'
