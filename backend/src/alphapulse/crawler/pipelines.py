import hashlib
import json
from typing import Any

from scrapy.crawler import Crawler
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import redis
from alphapulse.crawler.events import NEWS_CHANNEL
from alphapulse.models import NewsItem, NewsRevision

UPDATABLE_FIELDS = ("url", "title", "summary", "content_html", "content_text", "author", "tags",
                    "is_paywalled", "published_at", "source_updated_at", "extra")


def content_hash(item: dict[str, Any]) -> str:
    return hashlib.sha256("\0".join((item["title"], item["summary"], item["content_html"])).encode()).hexdigest()


class NewsPipeline:
    """按 (source_code, external_id) 幂等落库；内容变化写一条修订"""

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "NewsPipeline":
        return cls(crawler)

    async def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        spider = self.crawler.spider
        digest = content_hash(item)
        raw_response_id = item.pop("raw_response_id")
        locked = (
            select(NewsItem)
            .where(NewsItem.source_code == item["source_code"], NewsItem.external_id == item["external_id"])
            .with_for_update()
        )
        async with SessionLocal() as session:
            news = await session.scalar(locked)
            inserted_id = None
            if news is None:
                # 行不存在时 FOR UPDATE 锁不住任何东西；用 ON CONFLICT 处理同一条目被并发首次写入
                inserted_id = await session.scalar(
                    pg_insert(NewsItem)
                    .values(**item, content_hash=digest, revision_no=1)
                    .on_conflict_do_nothing(index_elements=[NewsItem.source_code, NewsItem.external_id])
                    .returning(NewsItem.id)
                )
                # 插入成功取新行；冲突说明另一处刚写入，按已存在条目走比较/修订流程
                news = await session.scalar(locked)
            if inserted_id is not None:
                kind = "new"
            else:
                # 主记录保存最新元数据；正文修订仍只由内容哈希变化决定。
                for field in UPDATABLE_FIELDS:
                    setattr(news, field, item[field])
                news.last_seen_at = func.now()
                if news.content_hash != digest:
                    kind = "revised"
                    news.content_hash = digest
                    news.revision_no += 1
                else:
                    kind = None

            if kind:
                session.add(NewsRevision(
                    news_id=news.id,
                    revision_no=news.revision_no,
                    title=item["title"],
                    summary=item["summary"],
                    content_html=item["content_html"],
                    content_hash=digest,
                    raw_response_id=raw_response_id,
                ))
            news_id = news.id
            await session.commit()

        spider.run.items_seen += 1
        if kind:
            setattr(spider.run, f"items_{kind}", getattr(spider.run, f"items_{kind}") + 1)
            await redis.publish(NEWS_CHANNEL, json.dumps({"id": news_id, "source_code": item["source_code"], "kind": kind}))
        return item
