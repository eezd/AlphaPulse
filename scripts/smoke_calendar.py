"""Exercise calendar persistence with synthetic credentials in the isolated CI database."""
import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path

from sqlalchemy import func, select

from alphapulse.calendar.jin10 import normalize
from alphapulse.calendar.jin10_history import calendar_history
from alphapulse.calendar.jin10_store import next_history_day, save_entry
from alphapulse.calendar.jin10_view import merge_items
from alphapulse.calendar.jin10_worker import ConnectionChanged, persist_day
from alphapulse.calendar.parsers import Scheduled
from alphapulse.calendar.service import release_rows, save_raw, save_result, save_schedule
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import EconomicRelease, EconomicRevision, RawResponse, Source
from alphapulse.models.jin10 import Jin10Connection, Jin10SyncDay


async def verify():
    assert os.environ["DATABASE_URL"].startswith("postgresql+asyncpg://ci:ci-only@"), "Calendar acceptance requires the isolated CI database"
    fixture = json.loads((Path("tests/fixtures/calendar/jin10.json")).read_text())
    now = datetime(2026, 11, 5, tzinfo=UTC)
    connected = now - timedelta(days=1)
    encrypted = encrypt("isolated-ci-calendar-account")
    async with SessionLocal() as session:
        row = await session.get(Jin10Connection, 1)
        row.token_encrypted, row.connected_at = encrypted, connected
        assert (await session.get(Source, "jin10")).collector == "calendar"
        await session.commit()
    day = date(2026, 11, 4)
    responses = {"data": fixture[str(day)], "event": fixture[f"event:{day}"], "holiday": fixture[f"holiday:{day}"]}
    await persist_day(day, responses, encrypted, connected, now)
    async with SessionLocal() as session:
        rows = list(await session.scalars(select(EconomicRelease).where(EconomicRelease.source == "jin10")))
        assert len(rows) == len(responses["data"]["list"]) + len(responses["event"]["list"])
        adp = next(r for r in rows if "jin10_78" in r.values)
        assert adp.first_received_at is None
        assert not adp.values["jin10_78"]["forecast_before_release"]
        assert adp.values["jin10_78"]["observed_at"]["forecast"] == now.isoformat()
        assert (await session.get(Jin10SyncDay, day)).counts == {"data": len(responses["data"]["list"]), "event": 8, "holiday": 0}
        before = await session.scalar(select(func.count()).select_from(EconomicRevision))
        assert await next_history_day(session, date(2025, 1, 1), day) == date(2025, 1, 1)
    await persist_day(day, responses, encrypted, connected, now + timedelta(minutes=1))
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(EconomicRevision)) == before
    try:
        await persist_day(day, responses, b"obsolete-credential", connected, now)
        raise AssertionError("Obsolete connection was accepted")
    except ConnectionChanged:
        pass
    invalid = deepcopy(responses)
    invalid["event"]["date"] = "2026-11-05"
    async with SessionLocal() as session:
        raw_before = await session.scalar(select(func.count()).select_from(RawResponse))
        coverage_before = (await session.get(Jin10SyncDay, day)).captured_at
    try:
        await persist_day(day, invalid, encrypted, connected, now + timedelta(hours=1))
        raise AssertionError("Mismatched response date was accepted")
    except ValueError:
        pass
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(RawResponse)) == raw_before + 3
        assert (await session.get(Jin10SyncDay, day)).captured_at == coverage_before

    # Join an official weekly release to the exact provider series, including its revised previous value.
    claims_day = date(2026, 10, 29)
    entries = normalize(fixture[str(claims_day)], "data", claims_day)
    claims = next(e for e in entries if "jin10_79" in e.values)
    async with SessionLocal() as session:
        raw = await save_raw(session, "jin10", "https://rili.jin10.com/day/2026-10-29/", 200, {}, b"isolated-claims", now)
        for entry in entries:
            await save_entry(session, entry, raw, now)
        official_raw = await save_raw(session, "dol", "https://www.dol.gov/claims.pdf", 200, {}, b"isolated-official", now)
        event = Scheduled("isolated-claims", "initial_claims", "Official initial claims", claims.at, "scheduled", "2026-10-24")
        await save_schedule(session, "dol", "https://oui.doleta.gov/unemploy/archive.asp", [event], official_raw, now, withdraw_missing=False)
        official = await session.scalar(select(EconomicRelease).where(EconomicRelease.source == "dol", EconomicRelease.external_id == event.external_id))
        save_result(session, official, {"initial_claims": {"actual": "19.40", "previous": "19.5"}}, "https://www.dol.gov/claims.pdf", official_raw, now)
        await session.commit()
        extra = next(r for r in await session.scalars(select(EconomicRelease).where(EconomicRelease.source == "jin10")) if "jin10_79" in r.values)
        item, = merge_items([(official, release_rows(official, now)[0])], [extra], now)
        assert item["actual"] == "19.40" and item["forecast"] == "19.9"
        assert item["previous_original"] == "19.4" and item["previous_revised"] == "19.5"
        assert item["field_sources"]["actual"]["source"] == "dol"
        assert item["field_sources"]["forecast"]["source"] == "jin10"
        assert item["forecast_before_release"] is False
        history = await calendar_history(session, official, "initial_claims", 36)
        assert len(history) == 1 and history[0]["actual"] == "19.40" and history[0]["forecast"] == "19.9"
        assert history[0]["first_recorded_actual"] == "19.40"
        assert history[0]["first_received_at"] == now
    print("Calendar acceptance passed: three categories, exact official joins, revisions, raw evidence, late forecasts, coverage and obsolete connection rejection")


async def main():
    try:
        await verify()
    finally:
        await engine.dispose()


asyncio.run(main())
