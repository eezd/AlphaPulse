import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.calendar.catalog import COUNTRIES, FAMILIES
from alphapulse.calendar.service import indicator_history, release_rows
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import EconomicRelease, EconomicRevision, EconomicSeriesValue, RawResponse

router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(require_admin)])
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


async def calendar_data(session: AsyncSession, start: datetime, end: datetime) -> dict:
    rows = list(await session.scalars(select(EconomicRelease).where(
        EconomicRelease.scheduled_at >= start, EconomicRelease.scheduled_at < end,
    ).order_by(EconomicRelease.scheduled_at, EconomicRelease.id)))
    raw = await redis.get(f"{KEY_PREFIX}calendar:status")
    status = json.loads(raw) if raw else None
    now = datetime.now(UTC)
    return {"items": [item for row in rows for item in release_rows(row, now)], "worker": status,
            "server_time": now, "timezone": "Asia/Shanghai",
            "countries": [{"code": code, "name": name} for code, name in COUNTRIES.items()],
            "forecast_available": any(row.values.get(key, {}).get("forecast") is not None
                                      for row in rows for key in FAMILIES[row.family].indicators)}


@router.get("")
async def list_calendar(session: Annotated[AsyncSession, Depends(get_session)],
                        start: date, days: Annotated[int, Query(ge=1, le=31)] = 7) -> ApiResponse[dict]:
    begin = datetime.combine(start, time.min, LOCAL_TZ)
    return success(await calendar_data(session, begin, begin + timedelta(days=days)))


@router.get("/{release_id}/revisions")
async def revisions(release_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[dict]]:
    if await session.get(EconomicRelease, release_id) is None:
        raise HTTPException(404, "日历事件不存在")
    rows = await session.scalars(select(EconomicRevision).where(EconomicRevision.release_id == release_id)
                                 .order_by(EconomicRevision.revision_no.desc()))
    return success([{"revision_no": r.revision_no, "captured_at": r.captured_at,
             "payload": r.payload, "raw_response_id": r.raw_response_id} for r in rows])


@router.get("/{release_id}/history")
async def history(release_id: int, indicator: str, session: Annotated[AsyncSession, Depends(get_session)],
                  limit: Annotated[int, Query(ge=1, le=120)] = 36) -> ApiResponse[list[dict]]:
    row = await session.get(EconomicRelease, release_id)
    if row is None:
        raise HTTPException(404, "日历事件不存在")
    if indicator not in FAMILIES[row.family].indicators:
        raise HTTPException(400, "指标不属于该事件")
    return success(await indicator_history(session, row, indicator, limit))


@router.get("/{release_id}/series")
async def series(release_id: int, indicator: str, session: Annotated[AsyncSession, Depends(get_session)],
                 limit: Annotated[int, Query(ge=1, le=120)] = 36) -> ApiResponse[list[dict]]:
    row = await session.get(EconomicRelease, release_id)
    if row is None:
        raise HTTPException(404, "日历事件不存在")
    if indicator not in FAMILIES[row.family].indicators:
        raise HTTPException(400, "指标不属于该事件")
    if row.period is None:
        return success([])
    # 此接口是当前官方序列，采集时间可以晚于所选事件；不得用作历史时点回测输入。
    rows = (await session.execute(select(EconomicSeriesValue, RawResponse.url).join(
        RawResponse, EconomicSeriesValue.raw_response_id == RawResponse.id,
    ).where(EconomicSeriesValue.source == row.source, EconomicSeriesValue.indicator == indicator,
            EconomicSeriesValue.period <= row.period).order_by(EconomicSeriesValue.period.desc()).limit(limit))).all()
    return success([{"period": value.period, "stage": value.stage, "value": value.value,
             "captured_at": value.captured_at, "source_url": url} for value, url in rows])
