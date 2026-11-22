from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.research_metadata import ContentStatus, ResearchArticleMetadata
from alphapulse.core.research_sources import SOURCES as RESEARCH_SOURCES
from alphapulse.api.response import ApiResponse, success
from alphapulse.core.auth import AuthSession, require_admin
from alphapulse.core.streams import session_events
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.crawler.events import NEWS_CHANNEL
from alphapulse.models import NewsBookmark, NewsItem, NewsRevision, Source
from alphapulse.briefings import admission
from alphapulse.briefings.models import Analysis
from alphapulse.briefings.queries import keyword_match

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


class AnalysisAdmission(BaseModel):
    mode: Literal['collect_only', 'archive', 'report']
    reason: str


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
    bookmarked: bool = False
    keywords: list = Field(default_factory=list)
    analysis: dict | None = None
    analysis_admission: AnalysisAdmission
    research: ResearchArticleMetadata | None = None
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


def news_filters(source: list[str] | None, q: str | None, start: datetime | None, end: datetime | None,
                 *, research_only: bool = False, author: str | None = None, content_status: ContentStatus | None = None) -> list:
    """新闻列表和 Chat 检索工具共用的筛选条件"""
    conditions = []
    if research_only:
        conditions.append(NewsItem.source_code.in_(RESEARCH_SOURCES))
    if author:
        escaped = author.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append(NewsItem.author.ilike(f'%{escaped}%', escape='\\'))
    if content_status:
        conditions.append(NewsItem.extra['research']['content_status'].as_string() == content_status)
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
        matches.append(select(Analysis.id).where(Analysis.news_id == NewsItem.id, Analysis.state == 'done',
                       keyword_match(Analysis.keywords, q)).correlate(NewsItem).exists())
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
    research_only: bool = False,
    author: str | None = None,
    content_status: ContentStatus | None = None,
) -> ApiResponse[NewsPage]:
    conditions = news_filters(source, q, start, end, research_only=research_only, author=author, content_status=content_status)
    if ids is not None:
        conditions.append(NewsItem.id.in_(ids))
    mine = select(NewsBookmark.news_id).where(NewsBookmark.username == principal.username)
    if bookmarked:
        conditions.append(NewsItem.id.in_(mine))
    if processing_state:
        if processing_state in ('pending', 'processing', 'interrupted', 'failed', 'done'):
            latest_state = (select(Analysis.state).where(Analysis.news_id == NewsItem.id)
                            .order_by(Analysis.version_no.desc()).limit(1).correlate(NewsItem).scalar_subquery())
            conditions.append(latest_state.in_(('failed', 'interrupted')) if processing_state == 'failed'
                              else latest_state == processing_state)
        elif processing_state == 'unprocessed':
            conditions.append(~select(Analysis.id).where(Analysis.news_id == NewsItem.id).correlate(NewsItem).exists())
        else:
            raise HTTPException(422, '未知处理状态')
    total = await session.scalar(select(func.count()).select_from(NewsItem).where(*conditions))
    rows = (await session.execute(
        select(NewsItem, func.left(NewsItem.content_text, EXCERPT_LENGTH).label("excerpt"))
        .where(*conditions)
        .order_by(NewsItem.published_at.asc() if sort == "asc" else NewsItem.published_at.desc(), NewsItem.id.asc() if sort == "asc" else NewsItem.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )).all()

    saved = set(await session.scalars(mine.where(NewsBookmark.news_id.in_([n.id for n, _ in rows]))))
    latest = {row.news_id: row for row in await session.scalars(select(Analysis).where(
        Analysis.news_id.in_([item.id for item, _ in rows])).distinct(Analysis.news_id)
        .order_by(Analysis.news_id, Analysis.version_no.desc()))}
    sources = {source.code: source for source in await session.scalars(select(Source).where(
        Source.code.in_({news.source_code for news, _ in rows})))}
    items = []
    for news, excerpt in rows:
        items.append(NewsBrief.model_validate({**news.__dict__, 'excerpt': excerpt, 'bookmarked': news.id in saved,
            'keywords': latest[news.id].keywords if news.id in latest else [],
            'analysis_admission': admission.view(news, sources.get(news.source_code)),
            'analysis': analysis_projection(latest.get(news.id), news), 'research': news.extra.get('research'),
            'published_at_precision': news.extra.get('published_at_precision'), 'published_date': news.extra.get('published_date')}))
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


def analysis_projection(row, news):
    if row is None:
        return None
    return {'id': row.id, 'version_no': row.version_no, 'state': row.state, 'source_revision': row.source_revision,
            'source_changed': row.source_revision is not None and row.source_revision != news.revision_no,
            'importance_stars': row.importance_stars, 'importance_score': row.importance_score,
            'result': row.output, 'error': row.error, 'time_basis': row.time_basis}


@router.get("/{news_id}")
async def get_news(news_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[NewsDetail]:
    news = await session.get(NewsItem, news_id)
    if news is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "新闻不存在")
    analysis = await session.scalar(select(Analysis).where(Analysis.news_id == news.id)
                                    .order_by(Analysis.version_no.desc()).limit(1))
    return success(NewsDetail.model_validate({**news.__dict__, "excerpt": news.content_text[:EXCERPT_LENGTH],
        'analysis_admission': admission.view(news, await session.get(Source, news.source_code)),
        'research': news.extra.get('research'),
        'analysis': analysis_projection(analysis, news), 'keywords': analysis.keywords if analysis else [],
        "published_at_precision": news.extra.get("published_at_precision"), "published_date": news.extra.get("published_date")}))


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
