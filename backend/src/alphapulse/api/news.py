from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.crawler.events import NEWS_CHANNEL
from alphapulse.models import NewsItem, NewsRevision

router = APIRouter(prefix="/api/news", tags=["news"], dependencies=[Depends(require_admin)])

EXCERPT_LENGTH = 300


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
    # 形如 "<published_at ISO>|<id>"，原样传回即可取下一页
    next_cursor: str | None


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


@router.get("")
async def list_news(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> NewsPage:
    stmt = select(NewsItem, func.left(NewsItem.content_text, EXCERPT_LENGTH).label("excerpt"))
    if source:
        stmt = stmt.where(NewsItem.source_code.in_(source))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(NewsItem.title.ilike(pattern), NewsItem.content_text.ilike(pattern)))
    if start:
        stmt = stmt.where(NewsItem.published_at >= start)
    if end:
        stmt = stmt.where(NewsItem.published_at < end)
    if cursor:
        published_at, news_id = cursor.rsplit("|", 1)
        stmt = stmt.where(tuple_(NewsItem.published_at, NewsItem.id) < (datetime.fromisoformat(published_at), int(news_id)))
    rows = (await session.execute(stmt.order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(limit))).all()

    items = [NewsBrief.model_validate({**news.__dict__, "excerpt": excerpt}) for news, excerpt in rows]
    next_cursor = f"{items[-1].published_at.isoformat()}|{items[-1].id}" if len(items) == limit else None
    return NewsPage(items=items, next_cursor=next_cursor)


@router.get("/stream", response_class=EventSourceResponse)
async def stream_news() -> AsyncIterator[ServerSentEvent]:
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(NEWS_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield ServerSentEvent(raw_data=message["data"], event="news")


@router.get("/{news_id}")
async def get_news(news_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> NewsDetail:
    news = await session.get(NewsItem, news_id)
    if news is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "新闻不存在")
    return NewsDetail.model_validate({**news.__dict__, "excerpt": news.content_text[:EXCERPT_LENGTH]})


@router.get("/{news_id}/revisions")
async def list_revisions(news_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> list[Revision]:
    rows = await session.scalars(
        select(NewsRevision).where(NewsRevision.news_id == news_id).order_by(NewsRevision.revision_no.desc())
    )
    return [Revision.model_validate(r, from_attributes=True) for r in rows]
