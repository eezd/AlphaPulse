"""DOL regression coverage uses a public release extract and isolated I/O."""
import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from alphapulse.calendar import dol

REPORT = (Path(__file__).parent / "fixtures/calendar/dol-current.txt").read_text()
SCHEDULE = """<html><body>
Publication Schedule: The UI Weekly Claims News Release is published each week on Thursday morning at 8:30am EST.
<table><thead><tr><th>Release Date</th><th>Release Time</th></tr></thead><tbody>
<tr><td>Wednesday, November 25, 2026</td><td>8:30 AM EST</td></tr>
</tbody></table></body></html>"""


def test_report_separates_statistical_weeks_and_revised_previous():
    initial, continued = (dol.report(REPORT)[family] for family in dol.FAMILIES)
    assert initial.at == continued.at == datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    assert initial.period == "2026-10-24" and continued.period == "2026-10-17"
    assert initial.values["initial_claims"]["actual"] == "19.4"
    assert initial.values["initial_claims"]["previous"] == "19.5"
    assert continued.values["continued_claims"]["actual"] == "170.1"
    assert continued.values["continued_claims"]["previous"] == "172.2"
    assert "修订" in continued.values["continued_claims"]["previous_basis"]


def test_report_resolves_new_year_from_publication_date():
    text = REPORT.replace("October 29, 2026", "January 2, 2027").replace("October 24", "December 26").replace("October 17", "December 19")
    result = dol.report(text)
    assert result["initial_claims"].period == "2026-12-26"
    assert result["continued_claims"].period == "2026-12-19"
    assert result["initial_claims"].at == datetime(2027, 1, 2, 13, 30, tzinfo=UTC)


def test_historical_report_keeps_explicit_unrevised_previous():
    # Source: https://oui.doleta.gov/press/2025/010825.pdf (first two pages).
    text = (Path(__file__).parent / "fixtures/calendar/dol-2025-01-08.txt").read_text()
    result = dol.report(text)
    assert result["initial_claims"].at == datetime(2025, 1, 8, 13, 30, tzinfo=UTC)
    assert result["initial_claims"].values["initial_claims"]["actual"] == "20.1"
    assert result["initial_claims"].values["initial_claims"]["previous"] == "21.1"
    assert result["continued_claims"].period == "2024-12-28"
    assert result["continued_claims"].values["continued_claims"]["previous"] == "183.4"


@pytest.mark.parametrize("text", [
    "Access denied",
    REPORT.replace("SEASONALLY ADJUSTED DATA", "CHANGED HEADER"),
    REPORT.replace("initial claims was 194,000", "initial claims pending"),
    REPORT.replace("October 24", "September 1"),
    REPORT.replace("195,000.", "unavailable."),
])
def test_unrecognized_report_does_not_fabricate_values(text):
    with pytest.raises(ValueError):
        dol.report(text)


def test_schedule_respects_dst_and_thanksgiving_exception():
    events = dol.schedule(SCHEDULE, datetime(2026, 10, 30, tzinfo=UTC))
    initial = {item.at.date(): item for item in events if item.family == "initial_claims"}
    assert initial[date(2026, 10, 29)].at.hour == 12
    assert initial[date(2026, 11, 5)].at.hour == 13
    assert initial[date(2026, 11, 25)].at.hour == 13
    assert date(2026, 11, 26) not in initial
    assert all(item.period is None for item in events)
    assert len({item.external_id for item in events}) == len(events)


@pytest.mark.parametrize("body", [
    SCHEDULE.replace("Thursday morning at 8:30am EST", "Schedule changed"),
    SCHEDULE.replace("8:30 AM EST", "9:00 AM EST"),
    SCHEDULE.replace("November 25, 2026", "11/25/2026"),
    SCHEDULE.replace("Release Date", "Changed heading"),
])
def test_changed_schedule_or_exception_format_fails_explicitly(body):
    with pytest.raises(ValueError):
        dol.schedule(body, datetime(2026, 10, 30, tzinfo=UTC))


def test_archives_only_follow_official_links_and_requested_range():
    body = '''<a href="/press/2025/010825.pdf">8</a><a href="/press/2025/010825.pdf">Duplicate</a>
    <a href="https://example.com/press/2025/011625.pdf">Other host</a>
    <a href="/press/2024/122624.pdf">Old</a>'''
    assert dol.archive_links(body, date(2025, 1, 1), date(2025, 12, 31)) == ["https://oui.doleta.gov/press/2025/010825.pdf"]
    with pytest.raises(ValueError, match="历史覆盖尚未确认"):
        dol.archive_links(body, date(2026, 1, 1), date(2026, 12, 31))
    with pytest.raises(ValueError, match="年份"):
        dol.archive_links('<a href="/press/2025/010826.pdf">Bad year</a>', date(2025, 1, 1), date(2026, 12, 31))


@pytest.mark.parametrize("offset,received,expected", [(0, False, "page"), (7, False, "pending"), (-7, True, "pending"), (-7, False, "error")])
def test_live_result_never_applies_another_week(trading, monkeypatch, offset, received, expected):
    from alphapulse.calendar import worker as module

    async def run():
        worker = module.CalendarWorker()
        try:
            response = httpx.Response(200, content=b"synthetic PDF", request=httpx.Request("GET", dol.LATEST))
            worker.fetch = AsyncMock(return_value=(response, 7))
            monkeypatch.setattr(module, "pdf_text", lambda body: REPORT)
            at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC) + timedelta(days=offset)
            row = SimpleNamespace(source="dol", family="initial_claims", release_url=None, scheduled_at=at,
                                  first_received_at=at if received else None)
            if expected == "error":
                with pytest.raises(ValueError, match="公告时间"):
                    await worker.result_page(row)
            else:
                page = await worker.result_page(row)
                assert (page is not None) == (expected == "page")
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_weekly_rows_show_their_own_week_and_uncertain_crypto_impact(trading):
    from alphapulse.calendar.service import release_rows
    announcement = dol.report(REPORT)["continued_claims"]
    row = SimpleNamespace(id=1, family="continued_claims", source="dol", title="Weekly claims",
                          period=announcement.period, stage=None, scheduled_at=announcement.at, schedule_status="scheduled",
                          first_received_at=announcement.at, last_checked_at=announcement.at, values=announcement.values,
                          error=None, release_url=dol.LATEST, schedule_url=dol.SCHEDULE, revision_no=2)
    [item] = release_rows(row, announcement.at)
    assert item["actual"] == "170.1" and item["unit"] == "万人"
    assert "2026-10-17" in item["event_name"]
    assert item["impact"] == "uncertain" and item["forecast"] is None


def test_history_preview_includes_both_series_without_writing(trading, monkeypatch):
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

    doc = backfill.Document("dol", dol.LATEST, b"%PDF synthetic report", datetime(2026, 10, 30, tzinfo=UTC))
    monkeypatch.setattr(backfill, "SessionLocal", Session)
    monkeypatch.setattr(backfill, "fetch_documents", AsyncMock(return_value=[doc]))
    monkeypatch.setattr(backfill, "pdf_text", lambda body: REPORT)
    save = AsyncMock()
    monkeypatch.setattr(backfill, "save_announcement", save)
    result = asyncio.run(backfill.run(date(2026, 10, 29), date(2026, 10, 29), sources={"dol"}))
    assert result["mode"] == "dry-run"
    assert result["announcements"] == {"initial_claims": 1, "continued_claims": 1}
    assert not result["errors"] and not result["missing_announcements"]
    save.assert_not_awaited()


def test_archive_year_gaps_are_explicit_and_current_report_is_still_collected(trading, monkeypatch):
    from alphapulse.calendar import backfill
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path.endswith("archive.asp"):
            return httpx.Response(200, text='<a href="/press/2025/010825.pdf">8</a>')
        return httpx.Response(200, content=b"%PDF synthetic report")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(backfill, "ensure_quiet_window", AsyncMock())
    monkeypatch.setattr(backfill.asyncio, "sleep", AsyncMock())
    errors = []
    documents = asyncio.run(backfill.fetch_documents(date(2025, 1, 1), date(2026, 10, 30), set(), errors, sources={"dol"}))
    assert len(documents) == 2 and dol.LATEST in requests
    assert len(errors) == 1 and "2026" in errors[0]["error"] and "不完整" in errors[0]["error"]
