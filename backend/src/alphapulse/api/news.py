from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.core.auth import AuthSession, require_admin
from alphapulse.core.streams import session_events
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.crawler.events import NEWS_CHANNEL
from alphapulse.models import NewsBookmark, NewsItem, NewsRevision
from alphapulse.news_research.models import Event, Membership, Package

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


class NewsRiskBrief(BaseModel):
    state: str
    stars: int | None = None
    summary: str | None = None
    verification: str | None = None


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
    risk: NewsRiskBrief | None = None
    bookmarked: bool = False
    keywords: list = Field(default_factory=list)
    events: list = Field(default_factory=list)
    published_at_precision: str | None = None
    published_date: str | None = None


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
    extra: dict | None = None
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
    principal: Annotated[AuthSession, Depends(require_admin)],
    source: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ids: Annotated[list[int] | None, Query(min_length=1, max_length=100)] = None,
    processing_state: str | None = None,
    bookmarked: bool = False,
    sort: Literal['asc', 'desc'] = 'desc',
) -> ApiResponse[NewsPage]:
    conditions = news_filters(source, q, start, end)
    if ids is not None:
        conditions.append(NewsItem.id.in_(ids))
    mine = select(NewsBookmark.news_id).where(NewsBookmark.username == principal.username)
    if bookmarked:
        conditions.append(NewsItem.id.in_(mine))
    if processing_state:
        current = (Package.news_id == NewsItem.id) & (Package.content_hash == NewsItem.content_hash)
        if processing_state == 'unprocessed':
            conditions.append(~select(Package.id).where(current).correlate(NewsItem).exists())
        elif processing_state == 'failed':
            conditions.append(select(Package.id).where(current, Package.state.in_(['index_failed', 'assign_failed'])).correlate(NewsItem).exists())
        elif processing_state in ['index_pending', 'index_processing', 'assign_pending', 'assign_processing', 'done']:
            conditions.append(select(Package.id).where(current, Package.state == processing_state).correlate(NewsItem).exists())
        else:
            raise HTTPException(422, '未知处理状态')
    total = await session.scalar(select(func.count()).select_from(NewsItem).where(*conditions))
    rows = (await session.execute(
        select(NewsItem, func.left(NewsItem.content_text, EXCERPT_LENGTH).label("excerpt"), Package)
        .outerjoin(Package, (Package.news_id == NewsItem.id) & (Package.content_hash == NewsItem.content_hash))
        .where(*conditions)
        .order_by(NewsItem.published_at.asc() if sort == "asc" else NewsItem.published_at.desc(), NewsItem.id.asc() if sort == "asc" else NewsItem.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )).all()

    saved = set(await session.scalars(mine.where(NewsBookmark.news_id.in_([n.id for n, _, _ in rows]))))
    package_ids = [p.id for _, _, p in rows if p]
    links = {}
    for pid, eid, title in await session.execute(select(Membership.package_id, Event.id, Event.title).join(Event, Event.id == Membership.event_id).where(Membership.package_id.in_(package_ids))):
        links.setdefault(pid, []).append({'id': eid, 'title': title})
    items = []
    for news, excerpt, package in rows:
        risk = {'state': package.state} if package else None
        items.append(NewsBrief.model_validate({**news.__dict__, 'excerpt': excerpt, 'risk': risk, 'bookmarked': news.id in saved, 'keywords': package.keywords if package else [], 'events': links.get(package.id, []) if package else [], 'published_at_precision': news.extra.get('published_at_precision'), 'published_date': news.extra.get('published_date')}))
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
    return success(NewsDetail.model_validate({**news.__dict__, "excerpt": news.content_text[:EXCERPT_LENGTH], "published_at_precision": news.extra.get("published_at_precision"), "published_date": news.extra.get("published_date")}))


@router.get("/{news_id}/revisions")
async def list_revisions(news_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[Revision]]:
    rows = await session.scalars(
        select(NewsRevision).where(NewsRevision.news_id == news_id).order_by(NewsRevision.revision_no.desc())
    )
    return success([Revision.model_validate(r, from_attributes=True) for r in rows])


@router.put("/{news_id}/bookmark")
async def bookmark(news_id: int, session: Annotated[AsyncSession, Depends(get_session)], principal: Annotated[AuthSession, Depends(require_admin)]):
    if await session.get(NewsItem, news_id) is None:
        raise HTTPException(404, "新闻不存在")
    await session.execute(insert(NewsBookmark).values(username=principal.username, news_id=news_id).on_conflict_do_nothing())
    await session.commit()
    return success(None)


@router.delete("/{news_id}/bookmark")
async def remove_bookmark(news_id: int, session: Annotated[AsyncSession, Depends(get_session)], principal: Annotated[AuthSession, Depends(require_admin)]):
    await session.execute(delete(NewsBookmark).where(NewsBookmark.username == principal.username, NewsBookmark.news_id == news_id))
    await session.commit()
    return success(None)
