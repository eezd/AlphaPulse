import asyncio
import gzip
import hashlib
from urllib.parse import urlparse

from scrapy.crawler import Crawler
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
