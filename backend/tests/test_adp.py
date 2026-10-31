"""Verify monthly ADP identity, signed changes, revisions and publication matching."""
import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from alphapulse.calendar import adp

FIXTURES = Path(__file__).parent / "fixtures/calendar"
SEPTEMBER = (FIXTURES / "adp-2026-09.html").read_text()
NEGATIVE = (FIXTURES / "adp-2025-06.html").read_text()
CROSS_YEAR = (FIXTURES / "adp-2024-12.html").read_text()
INDEX = (FIXTURES / "adp-index.html").read_text()


@pytest.mark.parametrize("body,period,actual,previous,at", [
    (SEPTEMBER, "2026-09", "9", "3.6", datetime(2026, 9, 30, 12, 15, tzinfo=UTC)),
    (NEGATIVE, "2025-06", "-3.3", "2.9", datetime(2025, 7, 2, 12, 15, tzinfo=UTC)),
    (CROSS_YEAR, "2024-12", "12.2", None, datetime(2025, 1, 8, 13, 15, tzinfo=UTC)),
])
def test_official_monthly_readings_preserve_signs_revisions_and_eastern_time(body, period, actual, previous, at):
    report = adp.report(body)
    assert (report.period, report.at) == (period, at)
    assert report.values["adp_jobs"]["actual"] == actual
    assert report.values["adp_jobs"]["previous"] == previous


def test_next_release_uses_explicit_dates_and_both_published_sentence_orders():
    next_month = adp.next_release(SEPTEMBER)
    assert next_month.period == "2026-10"
    assert next_month.at == datetime(2026, 11, 4, 13, 15, tzinfo=UTC)
    assert adp.next_release(NEGATIVE).at == datetime(2025, 7, 30, 12, 15, tzinfo=UTC)
    assert adp.next_release(CROSS_YEAR).period == "2025-01"
    events = adp.schedule(SEPTEMBER, "https://mediacenter.adp.com/report")
    assert events[0].external_id == "adp-2026-09"
    assert events[0].release_url == "https://mediacenter.adp.com/report"
    assert events[1].release_url is None


@pytest.mark.parametrize("body", [
    "Access denied",
    SEPTEMBER.replace("Report Highlights", "Other period"),
    SEPTEMBER.replace("Change in U.S. Private Employment", "Other metric"),
    SEPTEMBER.replace("Sep 30, 2026", "Sep 30, 2025"),
    SEPTEMBER.replace("8:15 a.m.", "Time pending"),
    SEPTEMBER.replace("September 2026 Report Highlights", "August 2026 Report Highlights"),
    SEPTEMBER.replace("90,000 Jobs", "91,000 Jobs"),
    SEPTEMBER.replace("Private-Sector Employment Increased", "Preliminary Estimate Employment Increased"),
], ids=["denied", "missing-period", "missing-reading", "wrong-year", "unknown-time", "wrong-month", "different-total", "weekly-report"])
def test_changed_or_inconsistent_report_fails(body):
    with pytest.raises(ValueError):
        adp.report(body)


def test_revisions_to_another_month_are_not_used_as_previous():
    body = SEPTEMBER.replace("The August total", "The July total")
    assert adp.report(body).values["adp_jobs"]["previous"] is None


def test_invalid_future_schedule_does_not_discard_independent_current_readings():
    body = (FIXTURES / "adp-2025-11.html").read_text()
    assert adp.report(body).period == "2025-11"
    assert adp.report(body).values["adp_jobs"]["actual"] == "-3.2"
    with pytest.raises(ValueError, match="下次公布日期与统计月不符"):
        adp.next_release(body)
    with pytest.raises(ValueError, match="相邻统计月"):
        adp.next_release(SEPTEMBER.replace("The October 2026 ADP", "The November 2026 ADP"))


def test_index_excludes_weekly_pulse_and_follows_actual_pagination():
    links, following = adp.release_links(INDEX, adp.INDEX, date(2025, 1, 1), date(2026, 10, 31))
    assert len(links) == 5
    assert all("Preliminary-Estimate" not in link for link in links)
    assert following.endswith("&o=9")
    links, following = adp.release_links(INDEX, adp.INDEX, date(2026, 10, 1), date(2026, 10, 31))
    assert links == []
    assert following is None
    links, following = adp.release_links(INDEX, adp.INDEX, date(2025, 1, 1), date(2025, 12, 31))
    assert links == []
    assert following is not None


@pytest.mark.parametrize("body", ["Access denied", INDEX.replace("https://mediacenter.adp.com/2026-09-30", "https://example.com/2026-09-30")])
def test_invalid_index_or_external_report_link_fails(body):
    with pytest.raises(ValueError):
        adp.release_links(body, adp.INDEX, date(2025, 1, 1), date(2026, 10, 31))


@pytest.mark.parametrize("period,release_url,outcome", [("2026-09", "saved-report", "success"), ("2026-08", "saved-report", "error"),
                                                       ("2026-10", None, "pending"), ("2026-09", None, "success")])
def test_worker_matches_actual_publication_and_statistical_month(trading, period, release_url, outcome):
    from alphapulse.calendar.worker import CalendarWorker

    async def run():
        worker = CalendarWorker()
        try:
            async def fetch(source, url):
                text = INDEX if url == adp.INDEX else SEPTEMBER
                return httpx.Response(200, text=text, request=httpx.Request("GET", url if url.startswith("https") else "https://mediacenter.adp.com/report")), 1
            worker.fetch = fetch
            row = SimpleNamespace(source="adp", period=period, release_url=release_url,
                                  scheduled_at=datetime(2026, 11, 4, 13, 15, tzinfo=UTC) if period == "2026-10" else datetime(2026, 9, 30, 12, 15, tzinfo=UTC))
            if outcome == "error":
                with pytest.raises(ValueError, match="所选日程不符"):
                    await worker.result_page(row)
            else:
                assert (await worker.result_page(row) is not None) == (outcome == "success")
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_history_preview_parses_stored_monthly_evidence_without_writes(trading, monkeypatch):
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

    doc = backfill.Document("adp", "https://mediacenter.adp.com/2026-09-30-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-90,000-Jobs-in-September", SEPTEMBER.encode(), datetime(2026, 10, 31, tzinfo=UTC))
    monkeypatch.setattr(backfill, "SessionLocal", Session)
    monkeypatch.setattr(backfill, "fetch_documents", AsyncMock(return_value=[doc]))
    save = AsyncMock()
    monkeypatch.setattr(backfill, "save_announcement", save)
    result = asyncio.run(backfill.run(date(2026, 9, 30), date(2026, 9, 30), sources={"adp"}))
    assert result["announcements"] == {"adp_jobs": 1}
    assert result["errors"] == []
    assert result["mode"] == "dry-run"
    save.assert_not_awaited()


def test_history_discovery_continues_past_newer_pages_without_matching_reports(trading, monkeypatch):
    from alphapulse.calendar import backfill

    historical = '<ul class="wd_item_list"><li class="wd_item"><div class="wd_date">Jan 8, 2025</div><div class="wd_title"><a href="/2025-01-08-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-122,000-Jobs-in-December">ADP National Employment Report: Private Sector Employment Increased by 122,000 Jobs in December</a></div></li></ul>'
    calls = []

    async def get(self, url, **kwargs):
        calls.append(url)
        body = CROSS_YEAR if "2025-01-08" in url else INDEX if url == adp.INDEX else historical
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(backfill, "ensure_quiet_window", AsyncMock())
    monkeypatch.setattr(backfill.asyncio, "sleep", AsyncMock())
    errors = []
    docs = asyncio.run(backfill.fetch_documents(date(2025, 1, 1), date(2025, 12, 31), set(), errors, sources={"adp"}))
    assert errors == []
    assert len(docs) == 1
    assert len(calls) == 3
    assert adp.report(docs[0].body.decode()).period == "2024-12"
