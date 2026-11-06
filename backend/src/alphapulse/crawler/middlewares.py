import asyncio
import gzip
import hashlib
from urllib.parse import urlparse

from scrapy.crawler import Crawler
from scrapy.exceptions import IgnoreRequest

from alphapulse.core.us_news import official_url
from scrapy.http import Request, Response
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import RawBody, RawResponse


class RedisRateLimitMiddleware:
    """按域名的跨进程限速：同一域名两次请求至少间隔 interval_ms"""

    interval_ms = 1000

    async def process_request(self, request: Request) -> None:
        if source := request.meta.get("official_source"):
            try:
                official_url(source, request.url)
            except ValueError as exc:
                raise IgnoreRequest(str(exc)) from exc
        if urlparse(request.url).hostname != "api.congress.gov":
            request.headers.pop("X-Api-Key", None)
        key = f"{KEY_PREFIX}rl:{urlparse(request.url).hostname}"
        while not await redis.set(key, "1", px=self.interval_ms, nx=True):
            ttl = await redis.pttl(key)
            await asyncio.sleep(max(ttl, 50) / 1000)


class RawResponseMiddleware:
    """每次响应都写 raw_responses；响应体按 sha256 去重存 raw_bodies"""

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "RawResponseMiddleware":
        return cls(crawler)

    async def process_response(self, request: Request, response: Response) -> Response:
        if response.body.lstrip().startswith(b'%PDF'):
            if not 200 <= response.status < 300:
                from scrapy.spidermiddlewares.httperror import HttpError
                raise HttpError(response)
            request.meta['policy_skipped'] = True
            raise IgnoreRequest('按采集规则不保存 PDF 响应')
        sha256 = hashlib.sha256(response.body).hexdigest()
        headers = {k.decode(): b", ".join(v).decode("latin-1") for k, v in response.headers.items()}
        async with SessionLocal() as session:
            await session.execute(
                pg_insert(RawBody).values(sha256=sha256, body=gzip.compress(response.body)).on_conflict_do_nothing()
            )
            raw_id = await session.scalar(
                insert(RawResponse).values(
                    run_id=request.meta.get("run_id"),
                    source_code=self.crawler.spider.name,
                    url=response.url,
                    method=request.method,
                    status=response.status,
                    headers=headers,
                    body_sha256=sha256,
                ).returning(RawResponse.id)
            )
            await session.commit()
        request.meta["raw_response_id"] = raw_id
        return response


class NewsContentPolicyMiddleware:
    """在下载前及响应头阶段阻止文件；此下载器仅由新闻采集使用。"""

    @classmethod
    def from_crawler(cls, crawler):
        from scrapy import signals
        obj = cls()
        crawler.signals.connect(obj.headers_received, signal=signals.headers_received)
        crawler.signals.connect(obj.bytes_received, signal=signals.bytes_received)
        return obj

    def process_request(self, request):
        from alphapulse.crawler.content_policy import file_url, SKIP_REASON
        if file_url(request.url) or request.meta.get('parsed'):
            request.meta['policy_skipped'] = True
            raise IgnoreRequest(SKIP_REASON)

    def headers_received(self, headers, request, **kwargs):
        from scrapy.exceptions import StopDownload
        from alphapulse.crawler.content_policy import binary_headers
        if binary_headers(headers):
            request.meta['policy_skipped'] = True
            raise StopDownload()

    def bytes_received(self, data, request, **kwargs):
        from scrapy.exceptions import StopDownload
        prefix = request.meta.get('_content_prefix', b'')
        if len(prefix) < 8:
            prefix = (prefix + data)[:8]
            request.meta['_content_prefix'] = prefix
            if prefix.lstrip().startswith(b'%PDF'):
                request.meta['policy_skipped'] = True
                raise StopDownload()
