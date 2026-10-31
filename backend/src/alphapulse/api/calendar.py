import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.api.response import ApiResponse, success
from alphapulse.calendar.catalog import COUNTRIES, FAMILIES
from alphapulse.calendar.service import release_rows
from alphapulse.calendar.jin10_history import calendar_history
from alphapulse.calendar.jin10_view import merge_items
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import EconomicRelease, EconomicRevision, EconomicSeriesValue, RawResponse
from alphapulse.models.jin10 import Jin10Connection, Jin10SyncDay

router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(require_admin)])
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


async def calendar_data(session: AsyncSession, start: datetime, end: datetime) -> dict:
    rows = list(await session.scalars(select(EconomicRelease).where(
        EconomicRelease.scheduled_at >= start, EconomicRelease.scheduled_at < end,
    ).order_by(EconomicRelease.scheduled_at, EconomicRelease.id)))
    raw = await redis.get(f"{KEY_PREFIX}calendar:status")
    status = json.loads(raw) if raw else None
    raw_jin10 = await redis.get(f"{KEY_PREFIX}calendar:jin10")
    jin10_status = json.loads(raw_jin10) if raw_jin10 else None
    if jin10_status:
        connection = await session.get(Jin10Connection, 1)
        if not connection or not connection.token_encrypted or jin10_status.get("connected_at") != connection.connected_at.isoformat():
            jin10_status = {**jin10_status, "state": "disconnected", "error": None}
    coverage = list(await session.scalars(select(Jin10SyncDay).where(
        Jin10SyncDay.day >= start.astimezone(LOCAL_TZ).date(), Jin10SyncDay.day < end.astimezone(LOCAL_TZ).date(),
    ).order_by(Jin10SyncDay.day)))
    now = datetime.now(UTC)
    items = merge_items([(row, item) for row in rows if row.source != "jin10" for item in release_rows(row, now)],
                        [row for row in rows if row.source == "jin10"], now)
    countries = {**COUNTRIES, **{item["country_code"]: item["country_name"] for item in items}}
    return {"items": items, "worker": status,
            "jin10": jin10_status,
            "coverage": [{"date": day.day, "captured_at": day.captured_at, "counts": day.counts} for day in coverage],
            "server_time": now, "timezone": "Asia/Shanghai",
            "countries": [{"code": code, "name": name} for code, name in countries.items()],
            "forecast_available": any(item["forecast"] is not None for item in items)}


def indicators(row: EconomicRelease) -> tuple[str, ...]:
    if row.source == "jin10":
        meta = row.values["_jin10"]
        return (meta["indicator"],) if meta["kind"] == "data" else ()
    return FAMILIES[row.family].indicators


@router.get("")
async def list_calendar(session: Annotated[AsyncSession, Depends(get_session)],
                        start: date, days: Annotated[int, Query(ge=1, le=31)] = 7) -> ApiResponse[dict]:
    begin = datetime.combine(start, time.min, LOCAL_TZ)
    return success(await calendar_data(session, begin, begin + timedelta(days=days)))


@router.get("/{release_id}/revisions")
async def revisions(release_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> ApiResponse[list[dict]]:
    row = await session.get(EconomicRelease, release_id)
    if row is None:
        raise HTTPException(404, "日历事件不存在")
    rows = await session.scalars(select(EconomicRevision).where(EconomicRevision.release_id == release_id)
                                 .order_by(EconomicRevision.revision_no.desc()))
    return success([{"source": row.source, "revision_no": r.revision_no, "captured_at": r.captured_at,
             "payload": r.payload, "raw_response_id": r.raw_response_id} for r in rows])


@router.get("/{release_id}/history")
async def history(release_id: int, indicator: str, session: Annotated[AsyncSession, Depends(get_session)],
                  limit: Annotated[int, Query(ge=1, le=120)] = 36) -> ApiResponse[list[dict]]:
    row = await session.get(EconomicRelease, release_id)
    if row is None:
        raise HTTPException(404, "日历事件不存在")
    if indicator not in indicators(row):
        raise HTTPException(400, "指标不属于该事件")
    return success(await calendar_history(session, row, indicator, limit))


@router.get("/{release_id}/series")
async def series(release_id: int, indicator: str, session: Annotated[AsyncSession, Depends(get_session)],
                 limit: Annotated[int, Query(ge=1, le=120)] = 36) -> ApiResponse[list[dict]]:
    row = await session.get(EconomicRelease, release_id)
    if row is None:
        raise HTTPException(404, "日历事件不存在")
    if indicator not in indicators(row):
        raise HTTPException(400, "指标不属于该事件")
    if row.period is None or row.source == "jin10":
        return success([])
    # 此接口是当前官方序列，采集时间可以晚于所选事件；不得用作历史时点回测输入。
    rows = (await session.execute(select(EconomicSeriesValue, RawResponse.url).join(
        RawResponse, EconomicSeriesValue.raw_response_id == RawResponse.id,
    ).where(EconomicSeriesValue.source == row.source, EconomicSeriesValue.indicator == indicator,
            EconomicSeriesValue.period <= row.period).order_by(EconomicSeriesValue.period.desc()).limit(limit))).all()
    return success([{"period": value.period, "stage": value.stage, "value": value.value,
             "captured_at": value.captured_at, "source_url": url} for value, url in rows])
