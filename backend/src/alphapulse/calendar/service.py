import gzip
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.calendar.catalog import COUNTRIES, FAMILIES, INDICATORS, STAGES
from alphapulse.calendar.parsers import Scheduled
from alphapulse.models import EconomicRelease, EconomicRevision, EconomicSeriesValue, RawBody, RawResponse


def next_poll(at: datetime, now: datetime, received: bool, failed: bool = False) -> datetime:
    age = (now - at).total_seconds()
    if age < 0:
        return at
    if received:
        seconds = 60 if age < 900 else 3600 if age < 86400 else 86400
    else:
        seconds = 5 if age < 120 else 15 if age < 900 else 60 if age < 7200 else 900
    if failed:
        seconds = max(seconds, 60)
    return now + timedelta(seconds=seconds)


async def save_raw(session: AsyncSession, source: str, url: str, status: int, headers: dict, body: bytes, now: datetime, *, method: str = "GET") -> int:
    digest = hashlib.sha256(body).hexdigest()
    await session.execute(insert(RawBody).values(sha256=digest, body=gzip.compress(body)).on_conflict_do_nothing())
    row = RawResponse(source_code=source, url=url, method=method, status=status, headers=headers,
                      body_sha256=digest, fetched_at=now)
    session.add(row)
    await session.flush()
    return row.id


def revision_payload(row: EconomicRelease) -> dict:
    return {"title": row.title, "period": row.period, "stage": row.stage,
            "scheduled_at": row.scheduled_at.isoformat(), "schedule_status": row.schedule_status,
            "release_url": row.release_url, "values": row.values,
            "first_received_at": row.first_received_at.isoformat() if row.first_received_at else None}


def record_revision(session: AsyncSession, row: EconomicRelease, raw_id: int) -> None:
    session.add(EconomicRevision(release_id=row.id, revision_no=row.revision_no,
                                payload=revision_payload(row), raw_response_id=raw_id))


async def save_schedule(session: AsyncSession, source: str, url: str, events: list[Scheduled], raw_id: int,
                        now: datetime, *, withdraw_missing: bool = True) -> None:
    existing = list(await session.scalars(select(EconomicRelease).where(EconomicRelease.schedule_url == url).with_for_update()))
    by_id = {r.external_id: r for r in existing}
    seen = set()
    for event in events:
        if event.at < datetime(2024, 12, 31, 16, tzinfo=UTC):
            continue
        seen.add(event.external_id)
        row = by_id.get(event.external_id)
        if row is None:
            # 归档回补先于 ICS 入库时，以正式时间和期次接管已有事件，保留主键和首次取值。
            matches = list(await session.scalars(select(EconomicRelease).where(
                EconomicRelease.source == source, EconomicRelease.family == event.family,
                EconomicRelease.scheduled_at == event.at,
            ).with_for_update()))
            matches = [r for r in matches if (event.period is None or r.period in (None, event.period))
                       and (event.stage is None or r.stage in (None, event.stage))]
            if len(matches) > 1:
                raise ValueError("日程匹配到多个同系列同时间事件")
            if matches:
                row = matches[0]
                row.external_id, row.schedule_url = event.external_id, url
        if row is None:
            row = EconomicRelease(source=source, external_id=event.external_id, family=event.family, title=event.title,
                                  scheduled_at=event.at, schedule_status=event.status, schedule_url=url,
                                  period=event.period, stage=event.stage,
                                  release_url=event.release_url,
                                  schedule_checked_at=now, next_check_at=max(now, event.at), values={}, revision_no=1)
            session.add(row)
            await session.flush()
            record_revision(session, row, raw_id)
        else:
            changed = (row.scheduled_at != event.at or row.title != event.title or row.schedule_status != event.status
                       or (event.period is not None and row.period != event.period)
                       or (event.stage is not None and row.stage != event.stage)
                       or (event.release_url is not None and row.release_url is None))
            row.schedule_checked_at = now
            if changed:
                row.scheduled_at, row.title, row.schedule_status = event.at, event.title, event.status
                if event.period is not None:
                    row.period = event.period
                if event.stage is not None:
                    row.stage = event.stage
                if row.release_url is None and event.release_url is not None:
                    row.release_url = event.release_url
                row.next_check_at = max(now, event.at)
                row.revision_no += 1
                record_revision(session, row, raw_id)
    # 仅撤回尚未公布的未来事件；过去记录不因官网滚动窗口而被删除。
    for row in existing:
        if withdraw_missing and row.external_id not in seen and row.scheduled_at > now and row.first_received_at is None and row.schedule_status != "withdrawn":
            row.schedule_status = "withdrawn"
            row.revision_no += 1
            record_revision(session, row, raw_id)


def save_result(session: AsyncSession, row: EconomicRelease, values: dict, url: str, raw_id: int, now: datetime,
                *, period: str | None = None, stage: str | None = None) -> None:
    if ((period is not None and row.period is not None and period != row.period)
            or (stage is not None and row.stage is not None and stage != row.stage)):
        raise ValueError("公告统计期或发布阶段与日历不符")
    if row.values != values or row.release_url != url or (period is not None and row.period != period) or (stage is not None and row.stage != stage):
        row.values = values
        row.release_url = url
        if period is not None:
            row.period = period
        if stage is not None:
            row.stage = stage
        if row.first_received_at is None:
            row.first_received_at = now
        row.revision_no += 1
        record_revision(session, row, raw_id)


async def save_series(session: AsyncSession, source: str, indicator: str, points: list[tuple[str, str]],
                      stage: str, raw_id: int, now: datetime) -> None:
    for period, value in points:
        stmt = insert(EconomicSeriesValue).values(source=source, indicator=indicator, period=period, stage=stage,
                                                  value=value, raw_response_id=raw_id, captured_at=now)
        await session.execute(stmt.on_conflict_do_update(
            index_elements=["source", "indicator", "period", "stage"],
            set_={"value": value, "raw_response_id": raw_id, "captured_at": now},
            where=EconomicSeriesValue.captured_at < now,
        ))


async def apply_bls_periods(session: AsyncSession, family: str, periods: dict[datetime, str], raw_id: int) -> None:
    rows = await session.scalars(select(EconomicRelease).where(EconomicRelease.source == "bls",
                                                               EconomicRelease.family == family,
                                                               EconomicRelease.scheduled_at.in_(periods)).with_for_update())
    for row in rows:
        period = periods[row.scheduled_at]
        if row.period != period:
            row.period = period
            row.revision_no += 1
            record_revision(session, row, raw_id)


def impact(key: str, actual: str | None, previous: str | None, forecast: str | None = None) -> tuple[str, str]:
    if actual is None:
        return "pending", "等待官方公布"
    reference = forecast if forecast is not None else previous
    if reference is None:
        return "uncertain", "已取得公布值，缺少可比前值和市场一致预期，暂不能判断影响。"
    basis = "市场预期" if forecast is not None else "前值"
    change = Decimal(actual) - Decimal(reference)
    movement = "上升" if change > 0 else "下降" if change < 0 else "持平"
    caveat = "未取得市场一致预期，这不是超预期判断。" if forecast is None else ""
    if not change:
        return "neutral", f"读数与{basis}一致，单项变化归为影响较小。{caveat}实际波动仍取决于其他数据和市场定价。"
    channel = INDICATORS[key].rate_channel
    if channel == "ambiguous":
        return "uncertain", f"读数较{basis}{movement}。政策、增长和汇率通道可能产生相反影响，暂不归为 BTC/ETH 的利多或利空。{caveat}"
    bullish = change < 0
    if key == "fed_rate":
        text = f"利率目标上限较{basis}{movement}，按利率与流动性通道参考判断。"
    else:
        text = f"通胀读数较{basis}{movement}，按通胀影响利率预期的通道参考判断。"
    return "bullish" if bullish else "bearish", text + caveat + "标签面向 BTC/ETH，不代表币价必然涨跌。"


def release_rows(row: EconomicRelease, now: datetime) -> list[dict]:
    if row.source == "jin10":
        from alphapulse.calendar.jin10_view import jin10_item
        return [jin10_item(row, now)]
    if row.schedule_status == "withdrawn":
        state = "withdrawn"
    elif row.first_received_at is not None:
        state = "released"
    elif now - row.scheduled_at > timedelta(days=45):
        state = "historical"
    elif row.error:
        state = "error"
    elif now < row.scheduled_at:
        state = "scheduled"
    else:
        state = "waiting" if now - row.scheduled_at < timedelta(minutes=2) else "delayed"
    result = []
    family = FAMILIES[row.family]
    country_name = COUNTRIES[family.country_code]
    period_name = ""
    if row.period is not None:
        if len(row.period) == 10:
            period_name = f"截至{row.period}当周"
        else:
            period_name = f"第{row.period[-1]}季度" if "-Q" in row.period else f"{int(row.period[5:7])}月"
    for key in family.indicators:
        indicator = INDICATORS[key]
        value = row.values.get(key, {})
        actual, previous = value.get("actual"), value.get("previous")
        forecast = value.get("forecast")
        direction, explanation = impact(key, actual, previous, forecast)
        unavailable = actual is None and value.get("unavailable_reason")
        if unavailable:
            direction, explanation = "uncertain", value["unavailable_reason"]
        result.append({"release_id": row.id, "indicator": key, "name": indicator.name, "unit": indicator.unit,
                       "country_code": family.country_code, "country_name": country_name,
                       "event_name": f"{country_name}{period_name}{indicator.name}{STAGES[row.stage] if row.stage else ''}",
                       "period": row.period, "stage": row.stage, "stage_name": STAGES[row.stage] if row.stage else None,
                       "title": row.title, "source": row.source, "scheduled_at": row.scheduled_at,
                       "importance": indicator.importance, "status": "unavailable" if unavailable and state == "released" else state,
                       "actual": actual, "previous": previous, "forecast": forecast,
                       "previous_basis": value.get("previous_basis", "官方本次报告列示前值（可能已修订）") if previous is not None else None,
                       "impact_basis": "forecast" if forecast is not None else "previous" if previous is not None else None,
                       "impact": direction, "explanation": explanation, "release_url": row.release_url,
                       "schedule_url": row.schedule_url, "first_received_at": row.first_received_at,
                       "last_checked_at": row.last_checked_at, "error": row.error, "revision_no": row.revision_no})
    return result


async def indicator_history(session: AsyncSession, row: EconomicRelease, key: str, limit: int) -> list[dict]:
    # 指标代码和所属发布系列共同限定历史，防止核心/整体、年率/月率以及初值/终值混在一起。
    first_actual = (select(EconomicRevision.payload["values"][key]["actual"].as_string())
                    .where(EconomicRevision.release_id == EconomicRelease.id,
                           EconomicRevision.payload["values"][key]["actual"].as_string().is_not(None))
                    .order_by(EconomicRevision.revision_no).limit(1).correlate(EconomicRelease).scalar_subquery())
    filters = []
    if row.source == "jin10":
        meta = row.values["_jin10"]
        filters = [EconomicRelease.values["_jin10"][field].as_string().is_not_distinct_from(meta[field])
                   for field in ("country_code", "name", "unit")]
    rows = (await session.execute(select(EconomicRelease, first_actual).where(
        EconomicRelease.source == row.source, EconomicRelease.family == row.family,
        EconomicRelease.stage.is_not_distinct_from(row.stage),
        EconomicRelease.scheduled_at <= row.scheduled_at,
        EconomicRelease.schedule_status != "withdrawn", EconomicRelease.first_received_at.is_not(None),
        EconomicRelease.values[key]["actual"].as_string().is_not(None),
        *filters,
    ).order_by(EconomicRelease.scheduled_at.desc(), EconomicRelease.id.desc()).limit(limit))).all()
    output = []
    for item, initial in rows:
        value = item.values[key]
        view = release_rows(item, datetime.now(item.scheduled_at.tzinfo))[0] if item.source == "jin10" else None
        output.append({"release_id": item.id, "scheduled_at": item.scheduled_at, "title": item.title,
                       "period": item.period, "stage": item.stage,
                       "actual": value["actual"], "previous": view["previous"] if view else value.get("previous"),
                       "forecast": value.get("forecast"), "first_recorded_actual": initial,
                       "first_received_at": item.first_received_at, "release_url": item.schedule_url if view else item.release_url,
                       "period_label": item.values["_jin10"]["period_label"] if view else None,
                       "forecast_before_release": value.get("forecast_before_release", False),
                       "forecast_observed_at": value.get("observed_at", {}).get("forecast"),
                       "revised": initial is not None and (initial != value["actual"] if view else Decimal(initial) != Decimal(value["actual"]))})
    return output
