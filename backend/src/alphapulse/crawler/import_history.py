"""导入已抓取的官方历史摘要及原始响应；独立任务不改变实时采集状态。

用法：python -m alphapulse.crawler.import_history SOURCE CAPTURES.jsonl.gz --start ISO时间
每行包含一次真实 HTTP 响应（source/url/status/headers/body/fetched_at）及解析后的 articles。
"""

import argparse
import base64
import gzip
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, HttpUrl
from scrapy import Request, Spider, signals
from scrapy.crawler import Crawler, CrawlerProcess
from scrapy.http import Response
from scrapy.utils.project import get_project_settings
from sqlalchemy import select, update
from twisted.python.failure import Failure

from alphapulse.core.db import SessionLocal
from alphapulse.core.official_sources import FEEDS, LANGUAGES
from alphapulse.crawler.events import write_logs
from alphapulse.crawler.middlewares import RawResponseMiddleware
from alphapulse.crawler.spiders.base import RunState
from alphapulse.models import CrawlRun, NewsItem, RawResponse

SOURCES = ("coindesk", "fed", "bea", "bls", *FEEDS)


class ArchivedArticle(BaseModel):
    guid: str = Field(min_length=1)
    url: HttpUrl
    title: str = Field(min_length=1)
    summary: str
    published_at: AwareDatetime
    source_updated_at: AwareDatetime | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: Literal["en", "de", "fr"] | None = None


class Capture(BaseModel):
    source: Literal["coindesk", "fed", "bea", "bls", "ecb", "eurostat", "boe", "ons", "destatis", "insee"]
    url: HttpUrl
    status: Literal[200]
    headers: dict[str, str]
    body: str
    body_encoding: Literal["utf-8", "base64"] = "utf-8"
    fetched_at: AwareDatetime
    articles: list[ArchivedArticle]


def article_item(source: str, article: ArchivedArticle, capture: Capture, raw_id: int) -> dict:
    content_html = f"<p>{escape(article.summary)}</p>" if article.summary else ""
    return {
        "source_code": source, "external_id": hashlib.sha256(article.guid.encode()).hexdigest(),
        "url": str(article.url), "title": article.title, "summary": article.summary,
        "content_html": content_html, "content_text": article.summary,
        "published_at": article.published_at, "source_updated_at": article.source_updated_at,
        "author": article.author, "tags": article.tags, "is_paywalled": False,
        "extra": {"language": article.language or LANGUAGES.get(source, "en"), "content_scope": "public_summary", "historical_backfill": True,
                  "archive_url": str(capture.url), "archive_fetched_at": capture.fetched_at.isoformat()},
        "raw_response_id": raw_id,
    }


def identity_url(url: str) -> str:
    # BEA 的 RSS GUID 同时使用 bea.gov 和 www.bea.gov；回补按原文链接识别已有文章。
    url = url.replace("https://bea.gov/", "https://www.bea.gov/").rstrip("/")
    # 官网跳转后的公报与 RSS 发布页属于同一公告；只统一导入去重键，保留原始链接。
    eurostat = re.fullmatch(r"https://ec\.europa\.eu/eurostat/(?:en/)?web/products-euro-indicators/w/([^/?#]+)", url)
    if eurostat:
        return f"https://ec.europa.eu/eurostat/product?code={eurostat[1]}"
    ons = re.fullmatch(r"https://www\.ons\.gov\.uk/.*/bulletins/(consumerpriceinflation|uklabourmarket|gdpfirstquarterlyestimateuk|quarterlynationalaccounts)/([^/?#]+)", url)
    if ons:
        series = {"consumerpriceinflation": "consumerpriceinflationuk", "quarterlynationalaccounts": "gdpquarterlynationalaccountsuk"}.get(ons[1], ons[1])
        return f"https://www.ons.gov.uk/releases/{series}{ons[2]}"
    return url


class HistoryImportSpider(Spider):
    name = "history_import"
    custom_settings = {
        "SCHEDULER": "scrapy.core.scheduler.Scheduler",
        "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
    }

    def __init__(self, source: str, captures: str, start: datetime, **kwargs):
        super().__init__(**kwargs)
        self.name = source
        self.captures = Path(captures)
        self.start_at = start
        self.end_at = datetime.now(UTC)
        self.run = RunState(uuid.uuid4(), "backfill", self.end_at, start)
        self.completed = False
        self.known_ids: set[str] = set()
        self.known_urls: set[str] = set()

    @classmethod
    def from_crawler(cls, crawler: Crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.on_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.on_error, signal=signals.spider_error)
        crawler.signals.connect(spider.on_error, signal=signals.item_error)
        crawler.signals.connect(spider.on_closed, signal=signals.spider_closed)
        return spider

    async def on_opened(self):
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(NewsItem.external_id, NewsItem.url).where(NewsItem.source_code == self.name)
            )).all()
            self.known_ids = {row[0] for row in rows}
            self.known_urls = {identity_url(row[1]) for row in rows}
            session.add(CrawlRun(id=self.run.id, source_code=self.name, trigger="backfill",
                                 started_at=self.run.started_at, status="running"))
            await session.commit()

    async def start(self):
        try:
            async for item in self.import_captures():
                yield item
        except Exception as exc:
            # start 的异常不走 spider_error 信号，必须让任务状态和退出码反映失败。
            self.run.errors.append(repr(exc))
            raise

    async def import_captures(self):
        raw_writer = RawResponseMiddleware(self.crawler)
        with gzip.open(self.captures, "rt", encoding="utf-8") as file:
            for line in file:
                capture = Capture.model_validate_json(line)
                if capture.source != self.name:
                    raise ValueError("历史响应来源与导入任务不一致")
                request = Request(str(capture.url), meta={"run_id": self.run.id})
                # 官方 PDF 归档需逐字节保存，不能经由文本解码破坏原始响应。
                body = base64.b64decode(capture.body, validate=True) if capture.body_encoding == "base64" else capture.body.encode()
                response = Response(str(capture.url), status=capture.status, headers=capture.headers,
                                    body=body, request=request)
                digest = hashlib.sha256(response.body).hexdigest()
                async with SessionLocal() as session:
                    raw_id = await session.scalar(select(RawResponse.id).where(
                        RawResponse.source_code == self.name, RawResponse.url == response.url,
                        RawResponse.body_sha256 == digest, RawResponse.fetched_at == capture.fetched_at,
                    ).limit(1))
                if raw_id is None:
                    await raw_writer.process_response(request, response)
                    raw_id = request.meta["raw_response_id"]
                    async with SessionLocal() as session:
                        await session.execute(update(RawResponse).where(RawResponse.id == raw_id)
                                              .values(fetched_at=capture.fetched_at))
                        await session.commit()
                self.run.requests += 1
                for article in capture.articles:
                    if not self.start_at <= article.published_at < self.end_at:
                        continue
                    item = article_item(self.name, article, capture, raw_id)
                    identity = identity_url(item["url"])
                    if item["external_id"] in self.known_ids or identity in self.known_urls:
                        continue
                    self.known_ids.add(item["external_id"])
                    self.known_urls.add(identity)
                    yield item

    def on_error(self, failure: Failure):
        self.run.errors.append(str(failure.value))

    async def on_closed(self, reason: str):
        status = "failed" if self.run.errors else ("success" if reason == "finished" else "interrupted")
        self.completed = status == "success"
        async with SessionLocal() as session:
            await session.execute(update(CrawlRun).where(CrawlRun.id == self.run.id).values(
                finished_at=datetime.now(UTC), status=status, requests=self.run.requests,
                items_seen=self.run.items_seen, items_new=self.run.items_new, items_revised=self.run.items_revised,
                error="\n".join(self.run.errors)[:4000] if self.run.errors else None,
            ))
            await session.commit()
        await write_logs([(self.name, self.run.id, "INFO" if status == "success" else "ERROR",
                           f"历史摘要导入 {status}：响应 {self.run.requests}，新增 {self.run.items_new}，修订 {self.run.items_revised}")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=SOURCES)
    parser.add_argument("captures", type=Path)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.start.tzinfo is None or args.start >= datetime.now(UTC):
        parser.error("起始时间须带时区且早于当前时间")
    process = CrawlerProcess(get_project_settings())
    crawler = process.create_crawler(HistoryImportSpider)
    process.crawl(crawler, source=args.source, captures=str(args.captures), start=args.start)
    process.start()
    if not crawler.spider.completed or crawler.spider.run.items_seen != crawler.spider.run.items_new:
        raise SystemExit("历史导入存在失败或意外修订，请检查采集日志")
    print(json.dumps({"source": args.source, "run_id": str(crawler.spider.run.id),
                      "requests": crawler.spider.run.requests, "inserted": crawler.spider.run.items_new}))


if __name__ == "__main__":
    main()
