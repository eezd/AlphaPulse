import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.core.auth import AuthSession, require_admin
from alphapulse.core.streams import session_events
from alphapulse.core.db import get_session
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.core.official_sources import REGIONS
from alphapulse.calendar.catalog import FAMILIES
from alphapulse.crawler.events import CONTROL_CHANNEL, HISTORY_SOURCES, LOG_CHANNEL, STATUS_KEY
from alphapulse.models import CrawlLog, CrawlRun, EconomicRelease, NewsItem, Source

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
    region_code: str | None
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


class CalendarSourceOut(BaseModel):
    code: str
    name: str
    region_code: str
    worker_alive: bool
    heartbeat_at: datetime | None
    schedule_checked_at: datetime | None
    schedule_success_at: datetime | None
    latest_result_at: datetime | None
    next_event_at: datetime | None
    overdue_count: int
    history_start: datetime | None
    history_end: datetime | None
    errors: list[str]


class SourcesOut(BaseModel):
    manager_alive: bool
    db_size_bytes: int
    sources: list[SourceOut]
    calendar_sources: list[CalendarSourceOut]


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


async def calendar_sources(session: AsyncSession) -> list[CalendarSourceOut]:
    now = datetime.now(UTC)
    raw = await redis.get(f"{KEY_PREFIX}calendar:status")
    heartbeat = json.loads(raw) if raw else None
    alive = bool(heartbeat and now - datetime.fromisoformat(heartbeat["at"]) < timedelta(seconds=30))
    pending = EconomicRelease.first_received_at.is_(None) & (EconomicRelease.schedule_status == "scheduled")
    published = EconomicRelease.first_received_at.is_not(None)
    rows = (await session.execute(select(
        EconomicRelease.source,
        func.max(EconomicRelease.schedule_checked_at).label("schedule_success_at"),
        func.max(EconomicRelease.first_received_at).label("latest_result_at"),
        func.min(EconomicRelease.scheduled_at).filter(pending & (EconomicRelease.scheduled_at > now)).label("next_event_at"),
        func.count().filter(pending & EconomicRelease.scheduled_at.between(now - timedelta(days=45), now)).label("overdue_count"),
        func.min(EconomicRelease.scheduled_at).filter(published).label("history_start"),
        func.max(EconomicRelease.scheduled_at).filter(published).label("history_end"),
    ).group_by(EconomicRelease.source))).mappings().all()
    stats = {row["source"]: dict(row) for row in rows}
    result_errors = {}
    for source, error in await session.execute(select(EconomicRelease.source, EconomicRelease.error).where(
        EconomicRelease.error.is_not(None), EconomicRelease.schedule_status == "scheduled",
        EconomicRelease.scheduled_at >= now - timedelta(days=45),
    ).order_by(EconomicRelease.last_checked_at.desc())):
        result_errors.setdefault(source, error)
    codes = {family.source for family in FAMILIES.values()}
    sources = list(await session.scalars(select(Source).where(Source.code.in_(codes)).order_by(Source.code)))
    output = []
    for source in sources:
        checks = [s for s in heartbeat["sources"].values() if s["source"] == source.code] if heartbeat else []
        errors = [s["error"] for s in checks if s["error"]]
        if source.code in result_errors:
            errors.append(result_errors[source.code])
        data = stats.get(source.code, {})
        output.append(CalendarSourceOut(
            code=source.code, name=source.name, region_code=REGIONS[source.code], worker_alive=alive,
            heartbeat_at=heartbeat["at"] if heartbeat else None,
            schedule_checked_at=max((s["checked_at"] for s in checks), default=None),
            **{key: data.get(key) for key in ("schedule_success_at", "latest_result_at", "next_event_at", "history_start", "history_end")},
            overdue_count=data.get("overdue_count", 0), errors=list(dict.fromkeys(errors)),
        ))
    return output


@router.get("/sources")
async def list_sources(session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[SourcesOut]:
    sources = (await session.scalars(select(Source).where(Source.collector == "news").order_by(Source.code))).all()
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
    return success(SourcesOut(
        manager_alive=bool(statuses),
        db_size_bytes=db_size,
        calendar_sources=await calendar_sources(session),
        sources=[
            SourceOut.model_validate({
                **s.__dict__,
                "region_code": REGIONS.get(s.code),
                "supports_history": s.code in HISTORY_SOURCES,
                "process": json.loads(statuses[s.code]) if s.code in statuses else None,
                "today_new": today_counts.get(s.code, (0, 0))[0],
                "today_revised": today_counts.get(s.code, (0, 0))[1],
                "total_items": totals.get(s.code, 0),
            })
            for s in sources
        ],
    ))


@router.patch("/sources/{code}")
async def patch_source(code: str, body: SourcePatch, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[None]:
    source = await session.get(Source, code)
    if source is None or source.collector != "news":
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
    return success(None)


@router.get("/runs")
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApiResponse[list[RunOut]]:
    stmt = select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(limit)
    if source:
        stmt = stmt.where(CrawlRun.source_code == source)
    return success([RunOut.model_validate(r, from_attributes=True) for r in await session.scalars(stmt)])


@router.get("/logs")
async def list_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    source: str | None = None,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> ApiResponse[list[LogOut]]:
    """按时间正序返回 before_id 之前最近的 limit 条"""
    stmt = select(CrawlLog).order_by(CrawlLog.id.desc()).limit(limit)
    if source:
        stmt = stmt.where(CrawlLog.source_code == source)
    if before_id:
        stmt = stmt.where(CrawlLog.id < before_id)
    rows = [LogOut.model_validate(r, from_attributes=True) for r in await session.scalars(stmt)]
    return success(rows[::-1])


@router.get("/logs/stream", response_class=EventSourceResponse)
async def stream_logs(principal: Annotated[AuthSession, Depends(require_admin)], source: str | None = None) -> AsyncIterator[ServerSentEvent]:
    async for item in session_events(_stream_logs(source), principal):
        yield item


async def _stream_logs(source: str | None = None) -> AsyncIterator[ServerSentEvent]:
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(LOG_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if source and json.loads(message["data"])["source_code"] != source:
                continue
            yield ServerSentEvent(raw_data=message["data"], event="log")


@router.post("/restart")
async def restart(body: RestartIn, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[None]:
    if body.source != "all":
        source = await session.get(Source, body.source)
        if source is None or source.collector != "news":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "来源不存在")
    receivers = await redis.publish(CONTROL_CHANNEL, json.dumps({"action": "restart", "source": body.source}))
    if receivers == 0:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "采集管理进程未运行")
    return success(None)
