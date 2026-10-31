"""Calendar tests use public fixtures and isolate all account, database and upstream I/O."""
import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alphapulse.calendar.jin10 import normalize, period

FIXTURE = json.loads((Path(__file__).parent / "fixtures/calendar/jin10.json").read_text())


def entries(day):
    return normalize(deepcopy(FIXTURE[day]), "data", date.fromisoformat(day))


@pytest.mark.parametrize("day", [key for key in FIXTURE if ":" not in key])
def test_real_values_and_timestamp_seconds(day):
    result = entries(day)
    assert len(result) == len(FIXTURE[day]["list"])
    for entry, raw in zip(result, FIXTURE[day]["list"], strict=True):
        assert entry.at.timestamp() == raw["pub_time_unix"]
        value = entry.values[f"jin10_{raw['indicator_id']}"]
        assert value["actual"] == raw["actual"]
        assert value["previous"] == raw["previous"]
        assert value["previous_revised"] == raw["revised"]
        assert "affect" not in value


def test_events_distinguish_midnight_from_undetermined_time():
    result = normalize(FIXTURE["event:2025-01-08"], "event", date(2025, 1, 8))
    ces = next(r for r in result if "CES" in r.title)
    trump = next(r for r in result if "特朗普" in r.title)
    assert ces.values["_jin10"]["precision"] == "tentative"
    assert trump.values["_jin10"]["precision"] == "minute"
    assert ces.at.hour == trump.at.hour == 16


def test_daily_holiday_and_empty_day():
    entry, = normalize(FIXTURE["holiday:2026-11-03"], "holiday", date(2026, 11, 3))
    assert entry.title == "文化日"
    assert entry.values["_jin10"]["exchange"] == "东京证券交易所"
    assert entry.values["_jin10"]["precision"] == "day"
    assert normalize(FIXTURE["holiday:2026-11-04"], "holiday", date(2026, 11, 4)) == []


@pytest.mark.parametrize("change", ["date", "duplicate", "timestamp", "number", "country"])
def test_invalid_contract_does_not_become_empty_success(change):
    body = deepcopy(FIXTURE["2026-11-04"])
    if change == "date": body["date"] = "2026-11-05"
    if change == "duplicate": body["list"] *= 2
    if change == "timestamp": body["list"][0]["pub_time_unix"] += 3600
    if change == "number": body["list"][0]["actual"] = 0
    if change == "country": body["list"][0]["country"] = 7
    with pytest.raises(ValueError):
        normalize(body, "data", date(2026, 11, 4))


@pytest.mark.parametrize("payload", [None, [], "not base64", "eJwDAAAAAAE=", "eJyLjo0FAAEVALk=garbage"])
def test_broken_holiday_payload_is_not_accepted(payload):
    with pytest.raises(ValueError):
        normalize({"date": "2026-11-03", "list": payload}, "holiday", date(2026, 11, 3))


def test_non_numeric_text_and_unknown_units_are_preserved():
    body = deepcopy(FIXTURE["2026-11-04"])
    body["list"] = body["list"][:1]
    body["list"][0].update(actual="4.00-4.25", unit=None, consensus=None, revised=None)
    entry, = normalize(body, "data", date(2026, 11, 4))
    assert entry.values["jin10_78"]["actual"] == "4.00-4.25"
    assert entry.values["_jin10"]["unit"] is None
    assert entry.values["jin10_78"]["forecast"] is None


def test_periods_cross_year_without_inventing_unrecognized_periods():
    at = datetime(2025, 1, 8, 15, tzinfo=UTC)
    assert period("12月", at) == "2024-12"
    assert period("至12月30日", at) == "2024-12-30"
    assert period("第四季度", at) == "2024-Q4"
    assert period("1-12月", at) is None
    assert period("至1月9日", at) is None


def stored(entry, id=11, now=None):
    return SimpleNamespace(id=id, source="jin10", family="jin10", title=entry.title, scheduled_at=entry.at,
                           external_id=entry.identity, schedule_status="scheduled", period=None, stage=None,
                           schedule_url="https://rili.jin10.com/day/2026-11-04/", release_url=None,
                           values=deepcopy(entry.values), first_received_at=now, last_checked_at=now,
                           schedule_checked_at=now, error=None, revision_no=1)


def test_late_forecast_is_displayed_but_not_a_historical_surprise(trading):
    from alphapulse.calendar.jin10_view import jin10_item
    row = stored(entries("2026-11-04")[0])
    row.values["jin10_78"].update(actual="9", forecast_before_release=False, observed_at={"forecast": "2026-11-05T00:00:00+00:00"})
    item = jin10_item(row, datetime(2026, 11, 5, tzinfo=UTC))
    assert item["forecast"] == "7.5"
    assert not item["forecast_before_release"]
    assert item["impact_basis"] == "previous" and item["impact"] == "neutral"


def test_exact_merge_prefers_official_fields_and_preserves_previous_revisions(trading):
    from alphapulse.calendar.jin10_view import merge_items
    entry = next(e for e in entries("2026-10-29") if "jin10_79" in e.values)
    extra = stored(entry)
    extra.values["jin10_79"].update(observed_at={"forecast": "2026-10-28T00:00:00+00:00"}, forecast_before_release=True)
    official = SimpleNamespace(source="dol", family="initial_claims", stage=None, period="2026-10-24", scheduled_at=entry.at,
                               first_received_at=entry.at, schedule_checked_at=entry.at, release_url="https://www.dol.gov/claims.pdf",
                               schedule_url="https://oui.doleta.gov/unemploy/archive.asp")
    original = {"release_id": 1, "indicator": "initial_claims", "actual": "19.40", "previous": "19.5", "forecast": None,
                "status": "released", "impact": "uncertain", "explanation": "official", "revision_no": 1, "scheduled_at": entry.at}
    item, = merge_items([(official, original)], [extra], entry.at)
    assert item["release_id"] == 1 and item["actual"] == "19.40"
    assert item["forecast"] == "19.9" and item["related_release_id"] == extra.id
    assert item["previous_original"] == "19.4" and item["previous_revised"] == "19.5"
    assert item["field_sources"]["actual"]["source"] == "dol"
    assert item["field_sources"]["forecast"]["source"] == "jin10"
    assert original["forecast"] is None
    official.period = "2026-10-17"
    assert len(merge_items([(official, original)], [extra], entry.at)) == 2
    official.period = "2026-10-24"
    extra.values["_jin10"]["unit"] = "人"
    assert len(merge_items([(official, original)], [extra], entry.at)) == 2


def test_cushing_is_not_total_commercial_crude(trading):
    from alphapulse.calendar.jin10_view import definition
    crude = next(e for e in entries("2025-01-08") if "jin10_111" in e.values)
    cushing = next(e for e in entries("2025-01-08") if "jin10_953" in e.values)
    assert definition(crude.values["_jin10"]) == ("eia_stocks", "eia_crude", None)
    assert definition(cushing.values["_jin10"]) is None


def test_repeated_storage_preserves_observation_and_first_received_time(trading):
    from alphapulse.calendar.jin10_store import save_entry
    now = datetime(2026, 11, 3, tzinfo=UTC)
    entry = entries("2026-11-04")[0]
    row = stored(entry)
    row.values["jin10_78"].update(observed_at={"previous": now.isoformat(), "forecast": now.isoformat()}, forecast_before_release=True)
    old = deepcopy(row.values)
    session = SimpleNamespace(scalar=AsyncMock(return_value=row), add=lambda r: pytest.fail("Unexpected revision"))
    asyncio.run(save_entry(session, entry, 1, now + timedelta(hours=1)))
    assert row.values == old and row.first_received_at is None


def test_reconnect_rejects_obsolete_inflight_capture(trading, monkeypatch):
    from alphapulse.calendar import jin10_worker as worker
    now = datetime.now(UTC)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        scalar = AsyncMock(return_value=SimpleNamespace(token_encrypted=b"new", connected_at=now))
        commit = AsyncMock()
    session = Session()
    monkeypatch.setattr(worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(worker, "save_raw", AsyncMock())
    response = {"data": FIXTURE["2026-11-04"], "event": FIXTURE["event:2026-11-04"], "holiday": FIXTURE["holiday:2026-11-04"]}
    with pytest.raises(worker.ConnectionChanged):
        asyncio.run(worker.persist_day(date(2026, 11, 4), response, b"old", now, now))
    worker.save_raw.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_every_verified_mapping_has_a_public_fixture_with_the_exact_definition(trading):
    from alphapulse.calendar.jin10_view import MAPPINGS, definition
    seen = set()
    for day in (key for key in FIXTURE if ":" not in key):
        for entry in entries(day):
            meta = entry.values["_jin10"]
            if definition(meta):
                seen.add((meta["country_code"], int(meta["indicator"].removeprefix("jin10_")), meta["name"]))
    expected = {(country, id_, name) for country, mapping in MAPPINGS.items() for id_, name in mapping}
    assert expected <= seen


def test_holidays_preserve_distinct_exchanges_within_the_same_country():
    rows = normalize(FIXTURE["holiday:2025-01-01"], "holiday", date(2025, 1, 1))
    assert len(rows) == 17
    us = [row for row in rows if row.values["_jin10"]["country_code"] == "US"]
    assert len(us) > 1 and len({row.identity for row in us}) == len(us)


def test_connection_status_never_accepts_a_previous_connection_heartbeat(trading):
    from alphapulse.api.jin10 import public_status
    now = datetime.now(UTC)
    row = SimpleNamespace(token_encrypted=b"synthetic", connected_at=now, error=None)
    sync = {"at": now.isoformat(), "state": "ready", "connected_at": now.isoformat(), "last_success_at": now.isoformat()}
    assert public_status(row, sync)["sync_state"] == "ready"
    sync["connected_at"] = (now - timedelta(seconds=1)).isoformat()
    assert public_status(row, sync)["sync_state"] == "awaiting_validation"
    row.token_encrypted = None
    sync["connected_at"] = now.isoformat()
    assert public_status(row, sync)["sync_state"] == "disconnected"


def test_account_login_does_not_prove_calendar_access():
    from alphapulse.calendar.jin10_browser import validate_access
    from alphapulse.calendar.jin10_auth import Jin10Error
    response = {"data": FIXTURE["2025-01-08"], "event": FIXTURE["event:2025-01-08"],
                "holiday": {"date": "2025-01-08", "list": FIXTURE["holiday:2026-11-04"]["list"]}}
    validate_access(response)
    response["data"] = {"date": "2025-01-08", "list": []}
    with pytest.raises(Jin10Error, match="读取权限"):
        validate_access(response)


@pytest.mark.parametrize("captured_at,completed", [
    ("2026-11-02T12:00:00+00:00", False),
    ("2026-11-04T15:59:59+00:00", False),
    ("2026-11-04T16:00:00+00:00", True),
])
def test_history_completion_requires_a_snapshot_after_the_shanghai_day(trading, captured_at, completed):
    from alphapulse.calendar.jin10_store import next_history_day
    day = date(2026, 11, 4)
    snapshot = SimpleNamespace(day=day, captured_at=datetime.fromisoformat(captured_at))
    session = SimpleNamespace(scalars=AsyncMock(return_value=[snapshot]))
    assert asyncio.run(next_history_day(session, day, day)) == (None if completed else day)
    assert asyncio.run(next_history_day(session, day, day, excluded={day})) is None


def test_withdrawal_retains_values_and_records_evidence_and_restoration(trading):
    from alphapulse.calendar.jin10_store import save_entry, withdraw_missing
    from alphapulse.calendar.jin10_view import jin10_item
    entry = entries("2026-11-04")[0]
    now = datetime(2026, 11, 3, tzinfo=UTC)
    row = stored(entry, now=now)
    revisions = []
    session = SimpleNamespace(scalars=AsyncMock(return_value=[row]), scalar=AsyncMock(return_value=row), add=revisions.append)
    before = deepcopy(row.values)
    asyncio.run(withdraw_missing(session, date(2026, 11, 4), "data", [], 42, now))
    assert row.schedule_status == "withdrawn" and row.values == before
    assert revisions[0].payload["schedule_status"] == "withdrawn" and revisions[0].raw_response_id == 42
    item = jin10_item(row, now)
    assert item["status"] == "withdrawn" and item["impact"] == "uncertain"
    asyncio.run(save_entry(session, entry, 43, now + timedelta(minutes=1)))
    assert row.schedule_status == "scheduled" and row.revision_no == 3
    assert row.first_received_at == now
    assert revisions[-1].payload["schedule_status"] == "scheduled" and revisions[-1].raw_response_id == 43


def test_withdrawn_provider_cannot_supply_official_fallback(trading):
    from alphapulse.calendar.jin10_view import merge_items
    entry = next(e for e in entries("2026-10-29") if "jin10_79" in e.values)
    extra = stored(entry)
    extra.schedule_status = "withdrawn"
    official = SimpleNamespace(source="dol", family="initial_claims", stage=None, period="2026-10-24", scheduled_at=entry.at,
                               first_received_at=entry.at, schedule_checked_at=entry.at, release_url="https://www.dol.gov/claims.pdf",
                               schedule_url="https://oui.doleta.gov/unemploy/archive.asp")
    original = {"release_id": 1, "indicator": "initial_claims", "actual": "19.40", "previous": None, "forecast": None,
                "status": "released", "scheduled_at": entry.at}
    items = merge_items([(official, original)], [extra], entry.at)
    result = next(item for item in items if item["release_id"] == 1)
    assert result["previous"] is None and result["forecast"] is None and result["related_release_id"] is None
    assert next(item for item in items if item["release_id"] == extra.id)["status"] == "withdrawn"


@pytest.mark.parametrize("failure_kind", ["contract", "browser", "missing_key"])
@pytest.mark.parametrize("failure_stage", ["fetch", "persist"])
def test_failed_days_do_not_starve_live_or_history_and_retry_after_backoff(trading, monkeypatch, failure_kind, failure_stage):
    from alphapulse.calendar import jin10_worker as worker
    from alphapulse.calendar.jin10_auth import Jin10Error
    now = datetime(2026, 11, 4, tzinfo=UTC)
    today, yesterday = now.date(), now.date() - timedelta(days=1)
    first_history = date(2025, 1, 1)
    connection = SimpleNamespace(token_encrypted=b"synthetic", connected_at=now)
    clock = [0]
    successful, attempted = set(), []
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None): return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, model, key):
            return connection if model is worker.Jin10Connection else SimpleNamespace(enabled=True)
    async def history(session, start, end, *, excluded):
        return next((first_history + timedelta(days=i) for i in range(3)
                     if first_history + timedelta(days=i) not in successful | excluded), None)
    def fail(day):
        if day in (yesterday, first_history):
            error = {"contract": ValueError, "browser": Jin10Error, "missing_key": KeyError}[failure_kind]
            raise error("synthetic day failure")
    async def fetch(day):
        attempted.append(day)
        if failure_stage == "fetch": fail(day)
        return {}
    async def persist(day, *args):
        if failure_stage == "persist": fail(day)
        successful.add(day)
        return {}
    async def sleep(seconds): clock[0] += seconds
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "decrypt", lambda value: "synthetic")
    monkeypatch.setattr(worker, "datetime", FrozenDatetime)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "next_history_day", history)
    monkeypatch.setattr(worker, "persist_day", persist)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    collector = worker.Jin10Worker()
    collector.browser = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), fetch_day=fetch)
    async def verify():
        for _ in range(30): await collector.step()
        assert {today + timedelta(days=i) for i in range(8)} <= successful
        assert first_history + timedelta(days=1) in successful and first_history + timedelta(days=2) in successful
        assert attempted.count(yesterday) == attempted.count(first_history) == 1
        clock[0] += 301
        for _ in range(30): await collector.step()
        assert attempted.count(yesterday) == attempted.count(first_history) == 2
    asyncio.run(verify())
