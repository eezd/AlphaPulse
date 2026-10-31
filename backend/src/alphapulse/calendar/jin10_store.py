"""Keep provider identities, raw evidence, field observation times and revisions together."""
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.calendar.jin10 import LOCAL, Entry, day_url
from alphapulse.calendar.service import record_revision
from alphapulse.models import EconomicRelease
from alphapulse.models.jin10 import Jin10SyncDay


async def save_entry(session: AsyncSession, entry: Entry, raw_id: int, now: datetime) -> None:
    row = await session.scalar(select(EconomicRelease).where(
        EconomicRelease.source == "jin10", EconomicRelease.external_id == entry.identity,
    ).with_for_update())
    values = {key: dict(value) for key, value in entry.values.items()}
    meta = values["_jin10"]
    key = meta["indicator"]
    if meta["kind"] == "data":
        current = values[key]
        old = row.values.get(key, {}) if row else {}
        observed = {}
        for field in ("actual", "previous", "forecast", "previous_revised"):
            if current[field] is not None:
                observed[field] = old.get("observed_at", {}).get(field, now.isoformat()) if current[field] == old.get(field) else now.isoformat()
        current["observed_at"] = observed
        forecast_at = datetime.fromisoformat(observed["forecast"]) if "forecast" in observed else None
        if forecast_at == now:
            before = current["actual"] is None
        else:
            before = old.get("forecast_before_release", False)
        current["forecast_before_release"] = bool(before and forecast_at and forecast_at < entry.at and meta["precision"] == "minute")
    received = meta["kind"] != "data" or values[key]["actual"] is not None
    if row is None:
        row = EconomicRelease(source="jin10", external_id=entry.identity, family="jin10", title=entry.title,
                              scheduled_at=entry.at, schedule_status="scheduled", schedule_url=day_url(date.fromisoformat(meta["date"])),
                              release_url=None, period=None, stage=None, schedule_checked_at=now, last_checked_at=now,
                              first_received_at=now if received else None, next_check_at=now, error=None,
                              values=values, revision_no=1)
        session.add(row)
        await session.flush()
        record_revision(session, row, raw_id)
        return
    changed = row.values != values or row.scheduled_at != entry.at or row.title != entry.title or row.schedule_status != "scheduled"
    row.schedule_checked_at = row.last_checked_at = now
    row.error = None
    if row.first_received_at is None and received:
        row.first_received_at = now
        changed = True
    if changed:
        row.title, row.scheduled_at, row.values = entry.title, entry.at, values
        row.schedule_status = "scheduled"
        row.schedule_url = day_url(date.fromisoformat(meta["date"]))
        row.revision_no += 1
        record_revision(session, row, raw_id)


async def withdraw_missing(session: AsyncSession, day: date, kind: str, entries: list[Entry], raw_id: int, now: datetime) -> None:
    # Only a fully validated response may withdraw records from its own day/category.
    seen = {entry.identity for entry in entries}
    rows = await session.scalars(select(EconomicRelease).where(
        EconomicRelease.source == "jin10",
        EconomicRelease.values["_jin10"]["date"].as_string() == day.isoformat(),
        EconomicRelease.values["_jin10"]["kind"].as_string() == kind,
        EconomicRelease.schedule_status != "withdrawn",
    ).with_for_update())
    for row in rows:
        if row.external_id not in seen:
            row.schedule_status = "withdrawn"
            row.schedule_checked_at = row.last_checked_at = now
            row.revision_no += 1
            record_revision(session, row, raw_id)


async def mark_day(session: AsyncSession, day: date, counts: dict, now: datetime) -> None:
    stmt = insert(Jin10SyncDay).values(day=day, captured_at=now, counts=counts)
    await session.execute(stmt.on_conflict_do_update(index_elements=["day"], set_={"captured_at": now, "counts": counts}))


async def next_history_day(session: AsyncSession, start: date, end: date, *, excluded: set[date] | None = None) -> date | None:
    from datetime import timedelta
    snapshots = await session.scalars(select(Jin10SyncDay).where(Jin10SyncDay.day.between(start, end)))
    # A preview captured before the local day ended is not a completed historical snapshot.
    covered = {row.day for row in snapshots if row.captured_at.astimezone(LOCAL).date() > row.day}
    day = start
    while day <= end:
        if day not in covered and day not in (excluded or set()):
            return day
        day += timedelta(days=1)
    return None
