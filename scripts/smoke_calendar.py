"""Exercise calendar persistence with synthetic credentials in the isolated CI database."""
import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alphapulse.calendar.jin10 import normalize
from alphapulse.calendar.jin10_history import calendar_history
from alphapulse.calendar.jin10_store import next_history_day, save_entry
from alphapulse.calendar.jin10_view import jin10_item, merge_items
from alphapulse.calendar.jin10_worker import ConnectionChanged, persist_day
from alphapulse.calendar.parsers import Scheduled
from alphapulse.calendar.service import release_rows, save_raw, save_result, save_schedule
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import EconomicRelease, EconomicRevision, RawResponse, Source
from alphapulse.models.jin10 import Jin10Connection, Jin10SyncDay


async def verify_results_pool():
    """Backlogged polling must leave connections for nested raw evidence writes."""
    from alphapulse.calendar import worker as module

    url = os.environ['DATABASE_URL']
    assert url.startswith('postgresql+asyncpg://ci:ci-only@')
    pool = create_async_engine(url, pool_size=1, max_overflow=4, pool_timeout=.2)
    sessions = async_sessionmaker(pool, expire_on_commit=False)
    now = datetime.now(UTC)
    seen = set()
    worker = module.CalendarWorker()

    async def result_page(row):
        # Match the separate evidence session used by CalendarWorker._fetch.
        await asyncio.sleep(.03)
        async with sessions() as session:
            await save_raw(session, row.source, 'https://example.invalid/pool', 200, {}, b'isolated-calendar-pool', now)
            await session.commit()
        seen.add(row.id)
        return None

    worker.result_page = result_page
    try:
        async with sessions() as session:
            session.add(Source(code='ci_pool', name='Calendar pool regression', list_url='https://example.invalid', interval_seconds=60, collector='calendar'))
            rows = [EconomicRelease(source='ci_pool', external_id=f'pool:{index}', family='pool', title='Pool regression',
                                    scheduled_at=now - timedelta(minutes=15), schedule_checked_at=now, schedule_url='https://example.invalid',
                                    next_check_at=now - timedelta(seconds=1)) for index in range(8)]
            session.add_all(rows)
            await session.commit()
            expected = {row.id for row in rows}
        with patch.object(module, 'SessionLocal', sessions):
            # One schedule transaction shares the real five-connection pool.
            async with pool.connect() as reserved:
                await reserved.execute(text('SELECT 1'))
                async with asyncio.timeout(5):
                    while not expected.issubset(seen):
                        await worker.results()
        async with sessions() as session:
            persisted = list(await session.scalars(select(EconomicRelease).where(EconomicRelease.id.in_(expected))))
            assert all(row.last_checked_at is not None and row.next_check_at > now for row in persisted)
        print('Calendar backlog and nested evidence connection acceptance passed', flush=True)
    finally:
        await worker.http.aclose()
        await worker.bls.aclose()
        async with pool.begin() as connection:
            for table in ('economic_releases', 'raw_responses', 'sources'):
                column = 'source' if table == 'economic_releases' else 'source_code' if table == 'raw_responses' else 'code'
                await connection.execute(text(f'DELETE FROM {table} WHERE {column}=:source'), {'source': 'ci_pool'})
        await pool.dispose()


async def verify():
    assert os.environ["DATABASE_URL"].startswith("postgresql+asyncpg://ci:ci-only@"), "Calendar acceptance requires the isolated CI database"
    await verify_results_pool()
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

    # Existing preview checkpoints must be revisited after downtime, without a schema migration.
    async with SessionLocal() as session:
        checkpoint = await session.get(Jin10SyncDay, day)
        checkpoint.captured_at = now - timedelta(days=2)
        await session.commit()
        assert await next_history_day(session, day, day) == day
        assert await next_history_day(session, day, day, excluded={day}) is None
    await persist_day(day, responses, encrypted, connected, now + timedelta(minutes=2))
    async with SessionLocal() as session:
        assert await next_history_day(session, day, day) is None
        other_day_raw = await save_raw(session, "jin10", "https://rili.jin10.com/day/2026-11-03/", 200, {}, b"isolated-other-dates", now)
        other_entries = [normalize(fixture["2025-01-08"], "data", date(2025, 1, 8))[0],
                         *normalize(fixture["holiday:2026-11-03"], "holiday", date(2026, 11, 3))]
        for entry in other_entries:
            await save_entry(session, entry, other_day_raw, now)
        await session.commit()
        revision_count = await session.scalar(select(func.count()).select_from(EconomicRevision))

    # Removing an economic release and event withdraws only those identities, once per change.
    removed = deepcopy(responses)
    adp_identity = adp.external_id
    removed["data"]["list"] = [r for r in removed["data"]["list"] if f"data:{r['data_id']}" != adp_identity]
    removed_event = removed["event"]["list"].pop(0)
    removed_ids = {adp_identity, f"event:{removed_event['id']}"}
    await persist_day(day, removed, encrypted, connected, now + timedelta(minutes=3))
    async with SessionLocal() as session:
        rows = list(await session.scalars(select(EconomicRelease).where(EconomicRelease.source == "jin10")))
        withdrawn = [r for r in rows if r.external_id in removed_ids]
        assert len(withdrawn) == 2 and all(r.schedule_status == "withdrawn" and r.revision_no == 2 for r in withdrawn)
        assert all(jin10_item(r, now)["status"] == "withdrawn" for r in withdrawn)
        assert all(r.schedule_status == "scheduled" and r.revision_no == 1 for r in rows if r.external_id not in removed_ids)
        evidence = list((await session.execute(select(EconomicRevision, RawResponse).join(
            RawResponse, RawResponse.id == EconomicRevision.raw_response_id,
        ).where(EconomicRevision.release_id.in_([r.id for r in withdrawn]), EconomicRevision.revision_no == 2))).all())
        assert len(evidence) == 2
        assert all(revision.payload["schedule_status"] == "withdrawn" and raw.url.endswith("#" + owner.external_id.split(":")[0])
                   for revision, raw in evidence for owner in withdrawn if owner.id == revision.release_id)
        assert await session.scalar(select(func.count()).select_from(EconomicRevision)) == revision_count + 2
    await persist_day(day, removed, encrypted, connected, now + timedelta(minutes=4))
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(EconomicRevision)) == revision_count + 2
    await persist_day(day, responses, encrypted, connected, now + timedelta(minutes=5))
    async with SessionLocal() as session:
        restored = list(await session.scalars(select(EconomicRelease).where(
            EconomicRelease.source == "jin10", EconomicRelease.external_id.in_(removed_ids),
        )))
        assert all(r.schedule_status == "scheduled" and r.revision_no == 3 for r in restored)
        assert next(r for r in restored if r.external_id == adp_identity).id == adp.id
        assert await session.scalar(select(func.count()).select_from(EconomicRevision)) == revision_count + 4

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
    print("Calendar acceptance passed: three categories, exact official joins, revisions, raw evidence, late forecasts, post-day coverage, scoped withdrawal/restoration and obsolete connection rejection")


async def main():
    try:
        await verify()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
