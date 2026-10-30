import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alphapulse.calendar.parsers import bea_release_link, parse_fed_calendar, parse_ics, parse_values

FIXTURES = Path(__file__).parent / "fixtures" / "calendar"


def ics(summary="Consumer Price Index", start="DTSTART;TZID=US-Eastern:20261110T083000", status=""):
    return f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:stable-id\r\nSUMMARY:{summary}\r\n{start}\r\n{status}\r\nEND:VEVENT\r\nEND:VCALENDAR"


@pytest.mark.parametrize("date,expected", [("20261014T083000", 12), ("20261110T083000", 13)])
def test_schedule_respects_eastern_dst(date, expected):
    [event] = parse_ics(ics(start=f"DTSTART;TZID=US-Eastern:{date}"), "bls")
    assert event.at.hour == expected
    assert event.external_id == "stable-id"


def test_schedule_unfolding_utc_and_cancellation():
    [event] = parse_ics(ics(summary="Personal Income and Outlays\\,\r\n September 2026",
                            start="DTSTART:20261029T123000Z", status="STATUS:CANCELLED"), "bea")
    assert event.family == "pce" and event.status == "withdrawn"
    assert event.title == "Personal Income and Outlays,September 2026"
    assert event.at == datetime(2026, 10, 29, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize("body,source", [("<html>Access denied</html>", "bea"),
                                        (ics(start="DTSTART:20261110T083000"), "bls"),
                                        (ics(summary="GDP by County and Personal Income by County, 2025"), "bea")])
def test_invalid_or_irrelevant_schedule_is_not_empty_success(body, source):
    with pytest.raises(ValueError):
        parse_ics(body, source)


def test_bea_discovery_matches_year_date_and_national_report():
    def row(title, link):
        return f'<tr class="scheduled-releases-type-press"><td class="release-date">September 30</td><td class="release-title"><a href="{link}">{title}</a></td></tr>'
    body = '<table>' + row("GDP, (Third Estimate)", "/news/2026/gdp-third") + row("GDP by County", "/news/2026/county") + row("Personal Income and Outlays, August", "/news/2026/pce") + row("GDP, (Third Estimate)", "/news/2025/old") + '</table>'
    assert bea_release_link(body, date(2026, 9, 30), "gdp") == "https://www.bea.gov/news/2026/gdp-third"
    assert bea_release_link(body, date(2026, 9, 30), "pce") == "https://www.bea.gov/news/2026/pce"
    assert bea_release_link(body, date(2026, 10, 29), "pce") is None
    with pytest.raises(ValueError, match="缺少数据行"):
        bea_release_link("<html>error</html>", date(2026, 9, 30), "pce")
    with pytest.raises(ValueError, match="不唯一"):
        bea_release_link(body + body, date(2026, 9, 30), "pce")


def test_fed_calendar_uses_published_time_not_press_conference():
    [event] = parse_fed_calendar((FIXTURES / "fed-month.html").read_text())
    assert event.at == datetime(2026, 10, 28, 18, tzinfo=UTC)
    assert event.family == "fomc"


@pytest.mark.parametrize("family,key,actual,previous", [
    ("cpi", "cpi_mom", "0.6", "0.4"), ("cpi", "core_cpi_yoy", "2.6", None),
    ("ppi", "ppi_yoy", "6.3", "5.6"), ("jobs", "payrolls", "2.9", "13.3"),
    ("jobs", "unemployment", "4.2", None), ("pce", "core_pce_mom", "0.2", "0.1"),
    ("pce", "pce_yoy", "3.4", None), ("pce", "core_pce_yoy", "3.0", None),
    ("fomc", "fed_rate", "4", None),
])
def test_official_release_values_and_revised_previous(family, key, actual, previous):
    values = parse_values(family, (FIXTURES / f"{family}.html").read_text())
    assert values[key] == {"actual": actual, "previous": previous}


@pytest.mark.parametrize("rate,expected", [("4-1/2", "4.5"), ("1/4", "0.25"), ("4.25", "4.25")])
def test_fed_fractional_upper_bound(rate, expected):
    html = f'<div id="article">maintain the target range for the federal funds rate at 4 to {rate} percent</div>'
    assert parse_values("fomc", html)["fed_rate"]["actual"] == expected


def test_employment_decrease_and_negative_gdp():
    html = '<div id="bodytext">Total nonfarm payroll employment declined by 92,000. The unemployment rate rose to 4.4 percent.</div>'
    assert parse_values("jobs", html)["payrolls"]["actual"] == "-9.2"
    html = '<div class="field--name-body">Real gross domestic product (GDP) decreased at an annual rate of 0.6 percent.</div>'
    assert parse_values("gdp", html)["gdp"]["actual"] == "-0.6"


def test_missing_release_value_is_not_zero():
    with pytest.raises((ValueError, KeyError)):
        parse_values("cpi", '<div id="bodytext">Release pending</div>')


def test_poll_cadence_is_fast_at_release_and_bounded_after_errors(trading):
    from alphapulse.calendar.service import next_poll
    at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    assert next_poll(at, at - timedelta(seconds=10), False) == at
    assert next_poll(at, at, False) == at + timedelta(seconds=5)
    assert next_poll(at, at + timedelta(minutes=5), False) == at + timedelta(minutes=5, seconds=15)
    assert next_poll(at, at, False, True) == at + timedelta(seconds=60)
    assert next_poll(at, at, True) == at + timedelta(seconds=60)


def test_without_consensus_labels_reference_impact_without_claiming_surprise(trading):
    from alphapulse.calendar.service import impact
    direction, reason = impact("cpi_mom", "0.6", "0.4")
    assert direction == "bearish" and "上升" in reason and "未取得市场一致预期" in reason
    assert "参考判断" in reason and "不是超预期判断" in reason and "不代表币价必然" in reason
    assert impact("cpi_mom", None, "0.4") == ("pending", "等待官方公布")


@pytest.mark.parametrize("key,actual,previous,forecast,direction", [
    ("cpi_mom", "0.2", "0.4", None, "bullish"),
    ("ppi_mom", "0.2", "0.2", None, "neutral"),
    ("fed_rate", "4.5", "4.25", None, "bearish"),
    ("cpi_mom", "0.3", "0.2", "0.4", "bullish"),
    ("payrolls", "2.9", "13.3", None, "uncertain"),
    ("core_cpi_yoy", "2.6", None, None, "uncertain"),
])
def test_impact_uses_forecast_first_and_does_not_hide_ambiguous_channels(trading, key, actual, previous, forecast, direction):
    from alphapulse.calendar.service import impact
    actual_direction, explanation = impact(key, actual, previous, forecast)
    assert actual_direction == direction
    if forecast is not None:
        assert "市场预期" in explanation and "不是超预期" not in explanation


def test_event_has_country_specific_importance_and_comparison_basis(trading):
    from alphapulse.calendar.service import release_rows
    now = datetime(2026, 10, 24, tzinfo=UTC)
    row = SimpleNamespace(id=1, period=None, stage=None, family="ppi", source="bls", title="PPI", scheduled_at=now,
                          schedule_status="scheduled", first_received_at=now, values={"ppi_mom": {"actual": "0.2", "previous": "0.1"}},
                          last_checked_at=now, error=None, release_url=None, schedule_url="https://www.bls.gov", revision_no=2)
    [mom, _] = release_rows(row, now)
    assert mom["country_code"] == "US" and mom["event_name"] == "美国PPI 最终需求月率"
    assert mom["importance"] == 3 and mom["impact"] == "bearish" and mom["impact_basis"] == "previous"
    row.family = "cpi"
    assert release_rows(row, now)[1]["importance"] == 5


def test_result_records_first_arrival_and_revisions_without_overwriting_history(trading):
    from alphapulse.calendar.service import save_result
    row = SimpleNamespace(id=1, period=None, stage=None, title="CPI", scheduled_at=datetime(2026, 10, 14, 12, 30, tzinfo=UTC),
                          schedule_status="scheduled", values={}, release_url=None, first_received_at=None, revision_no=1)
    captured = []
    session = SimpleNamespace(add=captured.append)
    now = row.scheduled_at + timedelta(seconds=7)
    first = {"cpi_mom": {"actual": "0.6", "previous": "0.4"}}
    save_result(session, row, first, "https://www.bls.gov/example", 1, now)
    save_result(session, row, first, "https://www.bls.gov/example", 2, now + timedelta(seconds=5))
    save_result(session, row, {"cpi_mom": {"actual": "0.5", "previous": "0.4"}}, "https://www.bls.gov/example", 3, now + timedelta(days=1))
    assert row.first_received_at == now and row.revision_no == 3
    assert len(captured) == 2 and captured[0].payload["values"] == first


def test_result_date_mismatch_waits_without_using_stale_release(trading):
    import httpx
    from alphapulse.calendar.worker import CalendarWorker
    async def check():
        worker = CalendarWorker()
        try:
            response = httpx.Response(200, text='<html><body>October 14, 2026</body></html>',
                                      request=httpx.Request("GET", "https://www.bls.gov/example"))
            worker.fetch = AsyncMock(return_value=(response, 1))
            row = SimpleNamespace(source="bls", family="cpi", scheduled_at=datetime(2026, 11, 10, 13, 30, tzinfo=UTC),
                                  first_received_at=None)
            assert await worker.result_page(row) is None
            row.scheduled_at = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
            with pytest.raises(ValueError, match="日期与日历不符"):
                await worker.result_page(row)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("source", ["bls", "umich"])
def test_rolling_page_rollover_preserves_received_result_without_source_error(trading, monkeypatch, source):
    import httpx
    from alphapulse.calendar import worker as module

    async def check():
        first_received = datetime(2026, 9, 24, 14, tzinfo=UTC)
        values = {"cpi_yoy" if source == "bls" else "umich_sentiment": {"actual": "2.5", "previous": "2.4"}}
        row = SimpleNamespace(id=1, source=source, family="cpi" if source == "bls" else "umich",
                              scheduled_at=first_received, first_received_at=first_received,
                              period="2026-09", stage="final" if source == "umich" else None,
                              values=values.copy(), revision_no=3, error="旧的采集错误")
        session = SimpleNamespace(scalar=AsyncMock(return_value=row), commit=AsyncMock())

        class Context:
            async def __aenter__(self):
                return session
            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(module, "SessionLocal", Context)
        html = ('<html><body>October 22, 2026</body></html>' if source == "bls" else
                '<h1>Preliminary Results for October 2026</h1><table id="front_table">'
                '<tr><td></td><td>Oct</td><td>Sep</td></tr><tr><td></td><td>2026</td><td>2026</td></tr>'
                '<tr><td>Index of Consumer Sentiment</td><td>49.0</td><td>48.1</td></tr></table>')
        worker = module.CalendarWorker()
        worker.fetch = AsyncMock(return_value=(httpx.Response(200, text=html,
                                request=httpx.Request("GET", "https://www.example.com/")), 9))
        try:
            await worker.poll_one(1)
            assert row.error is None
            assert row.first_received_at == first_received
            assert row.values == values and row.revision_no == 3
            session.commit.assert_awaited_once()
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(check())


def test_schedule_reschedule_and_withdrawal_preserve_past_and_revisions(trading):
    from alphapulse.calendar.parsers import Scheduled
    from alphapulse.calendar.service import save_schedule
    now = datetime(2026, 10, 24, tzinfo=UTC)
    def row(id_, at):
        return SimpleNamespace(id=id_, period=None, stage=None, external_id=str(id_), scheduled_at=at, title="CPI",
                               schedule_status="scheduled", first_received_at=None, values={}, release_url=None,
                               revision_no=1, schedule_checked_at=now, next_check_at=at)
    rescheduled = row(1, now + timedelta(days=2))
    removed = row(2, now + timedelta(days=3))
    past = row(3, now - timedelta(days=3))
    recorded = []
    session = SimpleNamespace(scalars=AsyncMock(return_value=[rescheduled, removed, past]), add=recorded.append)
    later = now + timedelta(days=4)
    async def check():
        events = [Scheduled("1", "cpi", "CPI", later)]
        await save_schedule(session, "bls", "https://www.bls.gov/calendar", events, 9, now)
        await save_schedule(session, "bls", "https://www.bls.gov/calendar", events, 10, now)
    asyncio.run(check())
    assert rescheduled.scheduled_at == later and rescheduled.next_check_at == later
    assert removed.schedule_status == "withdrawn" and past.schedule_status == "scheduled"
    assert len(recorded) == 2


def test_calendar_api_requires_auth_validates_range_and_exposes_unavailable_worker(trading, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from alphapulse.api import calendar
    from alphapulse.core.auth import require_admin, AuthSession
    from alphapulse.core.config import get_settings
    from alphapulse.core.db import get_session
    monkeypatch.setattr(get_settings(), "jwt_secret", "calendar-review-test-only-32-bytes")
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]), get=AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(calendar.router)
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(calendar.redis, "get", AsyncMock(return_value=None))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/api/calendar?start=2026-10-24")).status_code == 401
            assert (await client.get("/api/calendar/1/series?indicator=cpi_mom")).status_code == 401
            app.dependency_overrides[require_admin] = lambda: AuthSession(get_settings().admin_username, "calendar-test", 9999999999)
            response = await client.get("/api/calendar?start=2026-10-24")
            assert response.status_code == 200
            assert response.json()["data"]["worker"] is None and response.json()["data"]["forecast_available"] is False
            assert response.json()["data"]["countries"] == [{"code": "US", "name": "美国"}, {"code": "EA", "name": "欧元区"}, {"code": "GB", "name": "英国"}, {"code": "DE", "name": "德国"}, {"code": "FR", "name": "法国"}]
            assert (await client.get("/api/calendar?start=2026-10-24&days=32")).status_code == 422
            assert (await client.get("/api/calendar/999/revisions")).status_code == 404
            assert (await client.get("/api/calendar/999/history?indicator=cpi_mom")).status_code == 404
            assert (await client.get("/api/calendar/999/series?indicator=cpi_mom")).status_code == 404
            session.get.return_value = SimpleNamespace(family="cpi")
            assert (await client.get("/api/calendar/1/history?indicator=ppi_mom")).status_code == 400
            assert (await client.get("/api/calendar/1/history?indicator=cpi_mom&limit=121")).status_code == 422
            assert (await client.get("/api/calendar/1/series?indicator=ppi_mom")).status_code == 400
            assert (await client.get("/api/calendar/1/series?indicator=cpi_mom&limit=121")).status_code == 422
            session.get.return_value = SimpleNamespace(family="cpi", period=None)
            assert (await client.get("/api/calendar/1/series?indicator=cpi_mom")).json()["data"] == []
            handler = AsyncMock(return_value=[{"actual": "0.3", "first_recorded_actual": "0.4", "revised": True}])
            monkeypatch.setattr(calendar, "indicator_history", handler)
            history = await client.get("/api/calendar/1/history?indicator=cpi_mom")
            assert history.status_code == 200 and history.json()["data"][0]["revised"] is True
            assert handler.await_args.args[2:] == ("cpi_mom", 36)
    asyncio.run(check())


def test_worker_initializes_all_sources_and_drops_expired_month_status(trading):
    import httpx
    from alphapulse.calendar.worker import CalendarWorker
    async def check():
        worker = CalendarWorker()
        worker.sources["https://www.federalreserve.gov/old-month.htm"] = {"error": "old"}
        async def unavailable(source, url, *, query=None):
            assert len(worker.sources) == 17
            assert all(s["error"] for s in worker.sources.values())
            raise httpx.ConnectError("source unavailable")
        worker.fetch = unavailable
        try:
            await worker.schedules()
            assert all(s["error"] == "source unavailable" for s in worker.sources.values())
            assert sum(s["source"] == "fed" for s in worker.sources.values()) == 2
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("value,seconds", [("7200", 7200), ("Sun, 25 Oct 2026 14:00:00 GMT", 7200),
                                           ("Sun, 25 Oct 2026 11:00:00 GMT", -3600),
                                           (None, None), ("invalid", None), ("-1", None),
                                           ("25 Oct 2026 14:00:00", None), ("9" * 30, None)])
def test_retry_after_accepts_seconds_and_http_date_without_guessing_invalid_headers(trading, value, seconds):
    from alphapulse.calendar.worker import retry_after_at
    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert retry_after_at(value, now) == (now + timedelta(seconds=seconds) if seconds is not None else None)


@pytest.mark.parametrize("status", [429, 503])
def test_limited_result_preserves_response_and_defers_single_poll(trading, monkeypatch, status):
    import httpx
    from alphapulse.calendar import worker as module

    async def check():
        now = datetime.now(UTC)
        row = SimpleNamespace(id=1, source="eurostat", family="ea_hicp", scheduled_at=now,
                              first_received_at=None, release_url="https://ec.europa.eu/eurostat/product?code=2-16102026-ap")
        session = SimpleNamespace(scalar=AsyncMock(return_value=row), commit=AsyncMock())
        class Context:
            async def __aenter__(self):
                return session
            async def __aexit__(self, *args):
                pass
        monkeypatch.setattr(module, "SessionLocal", Context)
        save_raw = AsyncMock(return_value=1)
        monkeypatch.setattr(module, "save_raw", save_raw)
        calls = []
        def limited(request):
            calls.append(request)
            return httpx.Response(status, headers={"Retry-After": "7200"}, text="Quota exhausted")
        worker = module.CalendarWorker()
        await worker.http.aclose()
        worker.http = httpx.AsyncClient(transport=httpx.MockTransport(limited))
        try:
            await worker.poll_one(1)
            assert len(calls) == 1 and str(status) in row.error
            assert row.first_received_at is None
            assert row.next_check_at >= now + timedelta(seconds=7200)
            assert save_raw.await_args.args[3] == status
            assert save_raw.await_args.args[4]["retry-after"] == "7200"
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(check())


def test_schedule_honors_server_pause_without_blocking_other_sources(trading):
    import httpx
    from alphapulse.calendar.worker import CalendarWorker
    async def check():
        worker = CalendarWorker()
        calls = []
        async def unavailable(source, url, *, query=None):
            calls.append(source)
            if source == "ecb":
                httpx.Response(429, headers={"Retry-After": "7200"}, request=httpx.Request("GET", url)).raise_for_status()
            raise httpx.ConnectError("source unavailable")
        worker.fetch = unavailable
        try:
            await worker.schedules()
            await worker.schedules()
            assert calls.count("ecb") == 1
            assert calls.count("insee") == 2
            ecb = next(s for s in worker.sources.values() if s["source"] == "ecb")
            assert ecb["error"] and datetime.fromisoformat(ecb["retry_at"]) > datetime.now(UTC)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(check())
