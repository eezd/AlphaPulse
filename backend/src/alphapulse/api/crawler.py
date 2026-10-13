import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import redis
from alphapulse.crawler.events import CONTROL_CHANNEL, HISTORY_SOURCES, LOG_CHANNEL, STATUS_KEY
from alphapulse.models import CrawlLog, CrawlRun, NewsItem, Source

router = APIRouter(prefix="/api/crawler", tags=["crawler"], dependencies=[Depends(require_admin)])

# “今日”按北京时间计算
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


class ProcessStatus(BaseModel):
    state: Literal["running", "stopped", "restarting"]
    pid: int | None
    started_at: datetime | None
    next_run_at: datetime


class SourceOut(BaseModel):
    code: str
    name: str
    list_url: str
    enabled: bool
    interval_seconds: int
    backfill_hours: int
    consecutive_failures: int
    last_success_at: datetime | None
    last_error: str | None
    paused_until: datetime | None
    supports_history: bool
    history_target: datetime | None
    history_position: datetime | None
    history_done: bool
    # 管理进程未运行时为 None
    process: ProcessStatus | None
    today_new: int
    today_revised: int
    total_items: int


class SourcesOut(BaseModel):
    manager_alive: bool
    db_size_bytes: int
    sources: list[SourceOut]


class SourcePatch(BaseModel):
    enabled: bool | None = None
    interval_seconds: Annotated[int | None, Field(ge=5, le=86400)] = None
    # 设为时间表示开始或继续回补到该时间；显式传 null 表示取消
    history_target: AwareDatetime | None = None


class RunOut(BaseModel):
    id: uuid.UUID
    source_code: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    requests: int
    items_seen: int
    items_new: int
    items_revised: int
    error: str | None


class LogOut(BaseModel):
    id: int
    source_code: str
    run_id: uuid.UUID | None
    ts: datetime
    level: str
    message: str


class RestartIn(BaseModel):
    # 来源 code 或 "all"
    source: str


@router.get("/sources")
async def list_sources(session: Annotated[AsyncSession, Depends(get_session)]) -> SourcesOut:
    sources = (await session.scalars(select(Source).order_by(Source.code))).all()
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_counts = {
        code: (new or 0, revised or 0)
        for code, new, revised in await session.execute(
            select(CrawlRun.source_code, func.sum(CrawlRun.items_new), func.sum(CrawlRun.items_revised))
            .where(CrawlRun.started_at >= today)
            .group_by(CrawlRun.source_code)
        )
    }
    totals = dict((await session.execute(
        select(NewsItem.source_code, func.count()).group_by(NewsItem.source_code)
    )).all())
    statuses = await redis.hgetall(STATUS_KEY)
    db_size = await session.scalar(select(func.pg_database_size(func.current_database())))
    return SourcesOut(
        manager_alive=bool(statuses),
        db_size_bytes=db_size,
        sources=[
            SourceOut.model_validate({
                **s.__dict__,
                "supports_history": s.code in HISTORY_SOURCES,
                "process": json.loads(statuses[s.code]) if s.code in statuses else None,
                "today_new": today_counts.get(s.code, (0, 0))[0],
                "today_revised": today_counts.get(s.code, (0, 0))[1],
                "total_items": totals.get(s.code, 0),
            })
            for s in sources
        ],
    )


@router.patch("/sources/{code}")
async def patch_source(code: str, body: SourcePatch, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    source = await session.get(Source, code)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "来源不存在")
    for field, value in body.model_dump(exclude_none=True, exclude={"history_target"}).items():
        setattr(source, field, value)
    if "history_target" in body.model_fields_set:
        if code not in HISTORY_SOURCES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "该来源的接口不支持历史翻页，无法回补")
        if body.history_target is not None and body.history_target >= datetime.now(LOCAL_TZ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "回补目标时间必须早于现在")
        # 保留断点：改目标或重新开始时从已回补的位置继续
        source.history_target = body.history_target
        source.history_done = False
    await session.commit()


@router.get("/runs")
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunOut]:
    stmt = select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(limit)
    if source:
        stmt = stmt.where(CrawlRun.source_code == source)
    return [RunOut.model_validate(r, from_attributes=True) for r in await session.scalars(stmt)]


@router.get("/logs")
async def list_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: str | None = None,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[LogOut]:
    """按时间正序返回 before_id 之前最近的 limit 条"""
    stmt = select(CrawlLog).order_by(CrawlLog.id.desc()).limit(limit)
    if source:
        stmt = stmt.where(CrawlLog.source_code == source)
    if before_id:
        stmt = stmt.where(CrawlLog.id < before_id)
    rows = [LogOut.model_validate(r, from_attributes=True) for r in await session.scalars(stmt)]
    return rows[::-1]


@router.get("/logs/stream", response_class=EventSourceResponse)
async def stream_logs(source: str | None = None) -> AsyncIterator[ServerSentEvent]:
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(LOG_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if source and json.loads(message["data"])["source_code"] != source:
                continue
            yield ServerSentEvent(raw_data=message["data"], event="log")


@router.post("/restart")
async def restart(body: RestartIn, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    if body.source != "all" and await session.get(Source, body.source) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "来源不存在")
    receivers = await redis.publish(CONTROL_CHANNEL, json.dumps({"action": "restart", "source": body.source}))
    if receivers == 0:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "采集管理进程未运行")
