import asyncio
import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


def capture(articles, source="coindesk"):
    return {
        "source": source, "url": "https://www.coindesk.com/api/v1/articles/timeline?size=16",
        "status": 200, "headers": {"content-type": "application/json"}, "body": "{\"articles\":[]}",
        "fetched_at": "2026-10-22T04:00:00+00:00", "articles": articles,
    }


def article(**overrides):
    return {"guid": "original-guid", "url": "https://www.coindesk.com/markets/2025/01/02/story",
            "title": "Bitcoin announcement", "summary": "Public summary <script> is text & data",
            "published_at": "2025-01-02T08:30:00-05:00", "tags": ["Bitcoin"], **overrides}


def importer(module, tmp_path, data, source="coindesk"):
    path = tmp_path / "captures.jsonl.gz"
    with gzip.open(path, "wt") as file:
        file.write(json.dumps(data) + "\n")
    spider = module.HistoryImportSpider(source, str(path), datetime(2025, 1, 1, tzinfo=UTC))
    spider.crawler = SimpleNamespace(spider=spider)
    return spider


def test_archive_import_preserves_rss_identity_time_and_public_summary_provenance(trading):
    from scrapy.http import Request, XmlResponse

    from alphapulse.crawler.import_history import ArchivedArticle, Capture, article_item
    from alphapulse.crawler.spiders.rss import CoinDeskSpider

    response = XmlResponse("https://www.coindesk.com/rss", body=b'''<rss><channel><item>
        <guid>original-guid</guid><title>Bitcoin announcement</title>
        <link>https://www.coindesk.com/markets/2025/01/02/story</link>
        <pubDate>Thu, 02 Jan 2025 13:30:00 GMT</pubDate><description>Public summary</description>
        </item></channel></rss>''', request=Request("https://www.coindesk.com/rss", meta={"raw_response_id": 1}))
    rss_item, = CoinDeskSpider().parse_feed(response)
    data = Capture.model_validate(capture([article()]))
    item = article_item("coindesk", ArchivedArticle.model_validate(article()), data, 42)
    assert item["external_id"] == rss_item["external_id"]
    assert item["published_at"] == rss_item["published_at"]
    assert "<script>" not in item["content_html"] and "&lt;script&gt;" in item["content_html"]
    assert item["summary"] == item["content_text"] == article()["summary"]
    assert item["extra"]["historical_backfill"] is True
    assert item["extra"]["content_scope"] == "public_summary"
    assert item["extra"]["archive_fetched_at"] == data.fetched_at.isoformat()
    assert item["raw_response_id"] == 42


def test_archive_import_skips_existing_ids_links_and_out_of_range_articles(trading, monkeypatch, tmp_path):
    from alphapulse.crawler import import_history

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def scalar(self, statement):
            return 42

    monkeypatch.setattr(import_history, "SessionLocal", Session)
    data = capture([
        article(guid="known-id"), article(guid="other-id", url="https://bea.gov/news/2025/known"),
        article(guid="old", published_at="2024-12-31T10:00:00Z"),
        article(guid="future", published_at="2099-01-01T00:00:00Z"), article(), article(),
    ])
    spider = importer(import_history, tmp_path, data)
    spider.known_ids.add(hashlib.sha256(b"known-id").hexdigest())
    spider.known_urls.add("https://www.bea.gov/news/2025/known")
    async def run():
        return [item async for item in spider.start()]
    items = asyncio.run(run())
    assert len(items) == 1 and items[0]["external_id"] == hashlib.sha256(b"original-guid").hexdigest()
    assert not spider.run.errors


def test_pdf_archive_import_passes_original_bytes_to_raw_storage(trading, monkeypatch, tmp_path):
    from alphapulse.crawler import import_history

    body = b"%PDF-1.7\n%\xff\x80\xfe\x00\r\n"
    received = []

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def scalar(self, statement):
            assert hashlib.sha256(body).hexdigest() in statement.compile().params.values()
            return None
        async def execute(self, statement):
            pass
        async def commit(self):
            pass

    class RawWriter:
        def __init__(self, crawler):
            pass
        async def process_response(self, request, response):
            received.append(response.body)
            assert response.headers[b"Content-Type"] == b"application/pdf"
            request.meta["raw_response_id"] = 42
            return response

    monkeypatch.setattr(import_history, "SessionLocal", Session)
    monkeypatch.setattr(import_history, "RawResponseMiddleware", RawWriter)
    data = capture([article()], source="bls")
    data.update(url="https://www.dol.gov/newsroom/economicdata/cpi_01152025.pdf",
                headers={"content-type": "application/pdf"},
                body=base64.b64encode(body).decode(), body_encoding="base64")
    spider = importer(import_history, tmp_path, data, source="bls")

    async def run():
        return [item async for item in spider.start()]

    item, = asyncio.run(run())
    assert received == [body]
    assert item["raw_response_id"] == 42
    assert item["extra"]["archive_url"] == data["url"]
    assert not spider.run.errors


@pytest.mark.parametrize("broken", ["different_source", "malformed_capture", "invalid_base64", "unknown_encoding"])
def test_invalid_archive_input_fails_visibly_before_writing(trading, monkeypatch, tmp_path, broken):
    from alphapulse.crawler import import_history

    def unexpected_db_access():
        pytest.fail("invalid capture must not write to the database")
    monkeypatch.setattr(import_history, "SessionLocal", unexpected_db_access)
    data = capture([article()])
    if broken == "different_source":
        data["source"] = "fed"
    elif broken == "malformed_capture":
        data = {"source": "coindesk"}
    elif broken == "invalid_base64":
        data.update(body="%PDF-1.7", body_encoding="base64")
    else:
        data["body_encoding"] = "latin-1"
    spider = importer(import_history, tmp_path, data)
    async def run():
        return [item async for item in spider.start()]
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert spider.run.errors and not spider.completed


@pytest.mark.parametrize("source,feed_url,bulletin_url", [
    ("eurostat", "https://ec.europa.eu/eurostat/product?code=2-16102026-ap", "https://ec.europa.eu/eurostat/en/web/products-euro-indicators/w/2-16102026-ap"),
    ("eurostat", "https://ec.europa.eu/eurostat/product?code=2-16102026-ap", "https://ec.europa.eu/eurostat/web/products-euro-indicators/w/2-16102026-ap"),
    ("ons", "https://www.ons.gov.uk/releases/consumerpriceinflationukseptember2026", "https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/consumerpriceinflation/september2026"),
    ("ons", "https://www.ons.gov.uk/releases/uklabourmarketoctober2026", "https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/bulletins/uklabourmarket/october2026"),
    ("ons", "https://www.ons.gov.uk/releases/gdpquarterlynationalaccountsukapriltojune2026", "https://www.ons.gov.uk/economy/grossdomesticproductgdp/bulletins/quarterlynationalaccounts/apriltojune2026"),
    ("ons", "https://www.ons.gov.uk/releases/gdpfirstquarterlyestimateukapriltojune2026", "https://www.ons.gov.uk/economy/grossdomesticproductgdp/bulletins/gdpfirstquarterlyestimateuk/apriltojune2026"),
])
def test_import_skips_feed_release_alias_without_losing_other_announcements(trading, monkeypatch, tmp_path, source, feed_url, bulletin_url):
    from alphapulse.crawler import import_history

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def scalar(self, statement):
            return 42

    monkeypatch.setattr(import_history, "SessionLocal", Session)
    other_url = bulletin_url.replace("2026", "2025")
    data = capture([article(guid=bulletin_url, url=bulletin_url), article(guid=other_url, url=other_url)], source=source)
    data["url"] = bulletin_url
    spider = importer(import_history, tmp_path, data, source=source)
    spider.known_urls.add(import_history.identity_url(feed_url))
    async def run():
        return [item async for item in spider.start()]
    items = asyncio.run(run())
    assert len(items) == 1 and items[0]["url"] == other_url
    assert items[0]["extra"]["archive_url"] == bulletin_url
