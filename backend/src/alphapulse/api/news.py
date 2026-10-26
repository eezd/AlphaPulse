from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.core.auth import AuthSession, require_admin
from alphapulse.core.streams import session_events
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.crawler.events import NEWS_CHANNEL
from alphapulse.models import NewsItem, NewsRevision

router = APIRouter(prefix="/api/news", tags=["news"], dependencies=[Depends(require_admin)])

EXCERPT_LENGTH = 300
# 只扩展完整的常用主题词；其他输入继续按字面搜索，不翻译或改写任意查询。
NEWS_KEYWORDS = (
    ("比特币", "bitcoin", "btc"),
    ("以太坊", "ethereum", "ether", "eth"),
    ("稳定币", "stablecoin"),
    ("美联储", "federal reserve", "federal open market committee", "fomc"),
    ("消费者价格指数", "消费价格指数", "consumer price index", "cpi"),
    ("生产者价格指数", "producer price index", "ppi"),
    ("非农", "nonfarm", "employment situation"),
    ("国内生产总值", "gross domestic product", "gdp"),
    ("个人消费支出", "personal consumption expenditures", "pce"),
)


class NewsBrief(BaseModel):
    id: int
    source_code: str
    url: str
    title: str
    summary: str
    excerpt: str
    author: str | None
    tags: list
    is_paywalled: bool
    published_at: datetime
    first_seen_at: datetime
    revision_no: int


class NewsPage(BaseModel):
    items: list[NewsBrief]
    # 符合筛选条件的总条数，用于分页
    total: int


class NewsDetail(NewsBrief):
    content_html: str
    source_updated_at: datetime | None
    last_seen_at: datetime
    extra: dict


class Revision(BaseModel):
    revision_no: int
    title: str
    summary: str
    content_html: str
    captured_at: datetime


def news_filters(source: list[str] | None, q: str | None, start: datetime | None, end: datetime | None) -> list:
    """新闻列表和 Chat 检索工具共用的筛选条件"""
    conditions = []
    if source:
        conditions.append(NewsItem.source_code.in_(source))
    if q:
        keywords = next((group for group in NEWS_KEYWORDS if q.casefold() in group), (q,))
        matches = []
        for keyword in keywords:
            if keyword in ("btc", "eth", "ether"):
                # 短代码按单词匹配，避免把 method、Ethernet 等普通英文词误当作以太坊。
                pattern = f"(?i)(^|[^a-z0-9_]){keyword}([^a-z0-9_]|$)"
                matches.extend((NewsItem.title.regexp_match(pattern), NewsItem.content_text.regexp_match(pattern)))
                continue
            # 转义 LIKE 通配符；主题词扩展和普通查询都按字面匹配。
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            matches.extend((NewsItem.title.ilike(pattern, escape="\\"), NewsItem.content_text.ilike(pattern, escape="\\")))
        conditions.append(or_(*matches))
    if start:
        conditions.append(NewsItem.published_at >= start)
    if end:
        conditions.append(NewsItem.published_at < end)
    return conditions


@router.get("")
async def list_news(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ApiResponse[NewsPage]:
    conditions = news_filters(source, q, start, end)
    total = await session.scalar(select(func.count()).select_from(NewsItem).where(*conditions))
    rows = (await session.execute(
        select(NewsItem, func.left(NewsItem.content_text, EXCERPT_LENGTH).label("excerpt"))
        .where(*conditions)
        .order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )).all()

    items = [NewsBrief.model_validate({**news.__dict__, "excerpt": excerpt}) for news, excerpt in rows]
    return success(NewsPage(items=items, total=total))


@router.get("/stream", response_class=EventSourceResponse)
async def stream_news(principal: Annotated[AuthSession, Depends(require_admin)]) -> AsyncIterator[ServerSentEvent]:
    async for item in session_events(_stream_news(), principal):
        yield item


async def _stream_news() -> AsyncIterator[ServerSentEvent]:
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(NEWS_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield ServerSentEvent(raw_data=message["data"], event="news")


@router.get("/{news_id}")
async def get_news(news_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[NewsDetail]:
    news = await session.get(NewsItem, news_id)
    if news is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "新闻不存在")
    return success(NewsDetail.model_validate({**news.__dict__, "excerpt": news.content_text[:EXCERPT_LENGTH]}))


@router.get("/{news_id}/revisions")
async def list_revisions(news_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[Revision]]:
    rows = await session.scalars(
        select(NewsRevision).where(NewsRevision.news_id == news_id).order_by(NewsRevision.revision_no.desc())
    )
    return success([Revision.model_validate(r, from_attributes=True) for r in rows])
