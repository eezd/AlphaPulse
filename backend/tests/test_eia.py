"""Verify EIA units, calendar exceptions, archives and reporting boundaries."""
import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import httpx
import pytest

from alphapulse.calendar import eia

FIXTURES = Path(__file__).parent / "fixtures/calendar"
CSV = (FIXTURES / "eia-2026-10-23.csv").read_text()
SCHEDULE = (FIXTURES / "eia-schedule.html").read_text()


def test_official_csv_preserves_precision_units_and_excludes_spr():
    report = eia.report(CSV, SCHEDULE)
    assert report.at == datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    assert report.period == "2026-10-23"
    assert report.values["eia_crude"]["actual"] == "-189.200"
    assert report.values["eia_gasoline"]["actual"] == "387.000"
    assert report.values["eia_distillate"]["actual"] == "-146.200"
    assert all(v["previous"] is None for v in report.values.values())


def test_schedule_uses_holiday_times_and_eastern_dst():
    events = eia.schedule(SCHEDULE, datetime(2026, 10, 30, tzinfo=UTC))
    by_period = {item.period: item.at for item in events}
    assert by_period["2026-10-23"] == datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    assert by_period["2026-10-30"] == datetime(2026, 11, 4, 15, 30, tzinfo=UTC)
    assert by_period["2026-11-06"] == datetime(2026, 11, 12, 17, tzinfo=UTC)
    assert eia.release_at(date(2025, 12, 19), eia.exceptions(SCHEDULE)) == datetime(2025, 12, 29, 22, tzinfo=UTC)


@pytest.mark.parametrize("body", [
    CSV.replace('"Difference"', '"Changed heading"', 1),
    CSV.replace('"10/16/26"', '"10/15/26"', 1),
    CSV.replace('"Commercial (Excluding SPR)"', '"Other stock"'),
    CSV.replace('"-1.892"', '"NaN"', 1),
])
def test_unrecognized_csv_fails_instead_of_filling_other_data(body):
    with pytest.raises(ValueError):
        eia.report(body, SCHEDULE)


def test_following_supply_table_does_not_override_inventory_data():
    body = CSV + '\n"Commercial (Excluding SPR)",1,2,3,4,5,6,7'
    assert eia.report(body, SCHEDULE).values == eia.report(CSV, SCHEDULE).values


@pytest.mark.parametrize("body", ["Access denied", SCHEDULE.replace("10:30 a.m. eastern time on Wednesday", "Changed schedule"),
                                      SCHEDULE.replace("12:00 p.m.", "Time pending", 1)])
def test_unrecognized_schedule_fails(body):
    with pytest.raises(ValueError):
        eia.exceptions(body)


def test_archive_discovery_follows_published_report_and_csv_links():
    url = "https://www.eia.gov/petroleum/supply/weekly/archive/2026/2026_10_28/wpsr_2026_10_28.php"
    body = f'<a href="{url}">28</a><a href="https://example.com{urlparse(url).path}">Other host</a>'
    assert eia.archive_links(body, date(2026, 10, 1), date(2026, 10, 30)) == [url]
    assert eia.csv_link('<a href="download/table1.csv">CSV</a>', url) == url.rsplit("/", 1)[0] + "/download/table1.csv"
    with pytest.raises(ValueError):
        eia.archive_links(body, date(2025, 1, 1), date(2025, 12, 31))
    with pytest.raises(ValueError):
        eia.csv_link('<a href="other/table1.csv">Other file</a>', url)


@pytest.mark.parametrize("offset,received,expected", [(0, False, "page"), (7, False, "pending"), (-7, True, "pending"), (-7, False, "error")])
def test_worker_does_not_apply_latest_values_to_another_week(trading, offset, received, expected):
    from alphapulse.calendar.worker import CalendarWorker

    async def run():
        worker = CalendarWorker()
        try:
            worker.eia_schedule_body = SCHEDULE
            response = httpx.Response(200, text=CSV, request=httpx.Request("GET", eia.LATEST))
            worker.fetch = AsyncMock(return_value=(response, 1))
            at = datetime(2026, 10, 28, 14, 30, tzinfo=UTC) + timedelta(days=offset)
            row = SimpleNamespace(source="eia", family="eia_stocks", period="2026-10-23", release_url=None,
                                  scheduled_at=at, first_received_at=at if received else None)
            if expected == "error":
                with pytest.raises(ValueError, match="所选日程不符"):
                    await worker.result_page(row)
            else:
                result = await worker.result_page(row)
                assert (result is not None) == (expected == "page")
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("archive_date,valid", [("2026_10_28", True), ("2026_10_21", False)])
def test_history_preview_requires_matching_archive_date_without_writes(trading, monkeypatch, archive_date, valid):
    from alphapulse.calendar import backfill

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, _):
            return SimpleNamespace(all=lambda: [])

        async def scalars(self, _):
            return []

    now = datetime(2026, 10, 30, tzinfo=UTC)
    docs = [backfill.Document("eia", eia.SCHEDULE, SCHEDULE.encode(), now),
            backfill.Document("eia", f"https://www.eia.gov/petroleum/supply/weekly/archive/2026/{archive_date}/download/table1.csv", CSV.encode(), now)]
    monkeypatch.setattr(backfill, "SessionLocal", Session)
    monkeypatch.setattr(backfill, "fetch_documents", AsyncMock(return_value=docs))
    save = AsyncMock()
    monkeypatch.setattr(backfill, "save_announcement", save)
    result = asyncio.run(backfill.run(date(2026, 10, 28), date(2026, 10, 28), sources={"eia"}))
    assert result["mode"] == "dry-run"
    assert result["announcements"].get("eia_stocks", 0) == int(valid)
    assert bool(result["errors"]) != valid
    save.assert_not_awaited()
