import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from alphapulse.calendar.archives import parse_announcements, parse_bls_text
from alphapulse.calendar.parsers import bls_periods, release_metadata
from alphapulse.calendar.umich import current_result, monthly_series, parse_schedule


@pytest.mark.parametrize("title,period,stage", [
    ("Gross Domestic Product, 4th Quarter and Year 2024 (Advance Estimate)", "2024-Q4", "advance"),
    ("GDP (Third Estimate), Industries, 2nd Quarter 2026", "2026-Q2", "third"),
    ("Gross Domestic Product, 3rd Quarter 2025 (Initial Estimate)", "2025-Q3", "initial"),
    ("Gross Domestic Product, 3rd Quarter 2025 (Updated Estimate)", "2025-Q3", "updated"),
    ("Gross Domestic Product", None, None),
])
def test_quarter_and_stage_follow_explicit_official_title(title, period, stage):
    assert release_metadata("gdp", title) == (period, stage)


def test_bls_schedule_statistical_period_is_not_inferred_from_release_month():
    body = "<table><tr><td>September 2025</td><td>Oct. 24, 2025</td><td>08:30 AM</td></tr></table>"
    assert bls_periods(body) == {datetime(2025, 10, 24, 12, 30, tzinfo=UTC): "2025-09"}
    assert release_metadata("cpi", "Consumer Price Index") == (None, None)
    with pytest.raises(ValueError, match="统计月份"):
        bls_periods("<html>Access denied</html>")


def test_umich_historical_final_can_be_released_in_following_month():
    events = parse_schedule("Jul 2025 7/18/2025 8/1/2025\nDec 2025 12/5/2025 12/19/2025")
    assert events[1].period == "2025-07" and events[1].stage == "final"
    assert events[1].at == datetime(2025, 8, 1, 14, tzinfo=UTC)
    assert events[3].at.hour == 15  # 冬令时


def test_umich_annual_schedule_preserves_separate_stages():
    events = parse_schedule("RELEASE DATES FOR 2026\nJanuary 9 January Prelim\nJanuary 23 January Final")
    assert [(e.period, e.stage) for e in events] == [("2026-01", "preliminary"), ("2026-01", "final")]
    with pytest.raises(ValueError, match="重复"):
        parse_schedule("RELEASE DATES FOR 2026\nJanuary 9 January Prelim\nJanuary 9 January Prelim")
    with pytest.raises(ValueError):
        parse_schedule("Access denied")


def test_umich_current_heading_and_table_must_agree_and_previous_is_final():
    html = """<h1>Preliminary Results for October 2026</h1><table id="front_table">
    <tr><td></td><td>Oct</td><td>Sep</td></tr><tr><td></td><td>2026</td><td>2026</td></tr>
    <tr><td>Index of Consumer Sentiment</td><td>49.0</td><td>48.1</td></tr></table>"""
    period, stage, values = current_result(html)
    assert (period, stage) == ("2026-10", "preliminary")
    assert values["umich_sentiment"]["previous_basis"] == "2026-09 官方上月终值"
    with pytest.raises(ValueError, match="表头"):
        current_result(html.replace("<td>Oct</td>", "<td>Sep</td>"))


def test_current_series_has_no_invented_release_timestamp_and_rejects_duplicates():
    csv = "Month,YYYY,ICS_ALL\nDecember,2024,74.0\nJanuary,2025,71.7\n"
    assert monthly_series(csv) == [("2024-12", "74.0"), ("2025-01", "71.7")]
    with pytest.raises(ValueError, match="重复"):
        monthly_series(csv + "January,2025,72.0\n")
    with pytest.raises(ValueError, match="列名"):
        monthly_series("Month,YYYY,ICS\nJanuary,2025,71.7\n")


@pytest.mark.parametrize("text,payrolls,rate", [
    ("Total nonfarm payroll employment changed little in July (+73,000). The unemployment rate changed little at 4.2 percent.", "7.3", "4.2"),
    ("Total nonfarm payroll employment declined by 92,000. The unemployment rate edged up to 4.4 percent.", "-9.2", "4.4"),
    ("Total nonfarm payroll employment increased by 177,000. The unemployment rate was unchanged at 4.2 percent.", "17.7", "4.2"),
    ("Total nonfarm payroll employment edged down by 92,000. The unemployment rate changed little at 4.4 percent.", "-9.2", "4.4"),
    ("Total nonfarm payroll employment edged up by 119,000. The unemployment rate, at 4.4 percent, changed little.", "11.9", "4.4"),
])
def test_archived_jobs_wording_units_and_negative_values(text, payrolls, rate):
    values = parse_bls_text("jobs", text)
    assert values["payrolls"]["actual"] == payrolls and values["unemployment"]["actual"] == rate


def test_pce_combined_release_has_two_periods_and_no_false_previous():
    html = b'''<html><body>January 22, 2026 10:00 a.m.
    <h1>Personal Income and Outlays, October and November 2025</h1>
    <div class="field--name-body"><p>From the same month one year ago, the PCE price index increased 2.7 percent in October,
    followed by an increase of 2.8 percent in November. Excluding food and energy, the PCE price index also increased
    2.7 percent followed by an increase of 2.8 percent.</p></div>
    <table><tr><td></td><td>October</td><td>November</td></tr>
    <tr><td>PCE price index</td><td>0.2</td><td>0.2</td></tr>
    <tr><td>PCE price index, excluding food and energy</td><td>0.2</td><td>0.2</td></tr></table></body></html>'''
    first, second = parse_announcements("pce", html)
    assert (first.period, second.period) == ("2025-10", "2025-11")
    assert first.at == second.at == datetime(2026, 1, 22, 15, tzinfo=UTC)
    assert first.values["pce_yoy"]["actual"] == "2.7" and second.values["pce_yoy"]["actual"] == "2.8"
    assert all(v["previous"] is None for a in (first, second) for v in a.values.values())
    with pytest.raises(ValueError, match="表头"):
        parse_announcements("pce", html.replace(b"<td>October</td>", b"<td>September</td>"))


def test_pce_rounded_zero_comes_from_official_table_not_imprecise_prose():
    from alphapulse.calendar.parsers import parse_values
    html = '''<div class="field--name-body"><p>From the preceding month, the PCE price index for March decreased less than 0.1 percent.
    Excluding food and energy, the PCE price index increased less than 0.1 percent.</p>
    <p>From the same month one year ago, the PCE price index increased 2.3 percent. Excluding food and energy,
    the PCE price index increased 2.6 percent.</p></div><table><tr><td>PCE price index</td><td>0.0</td></tr>
    <tr><td>PCE price index, excluding food and energy</td><td>0.0</td></tr></table>'''
    assert parse_values("pce", html)["pce_mom"] == {"actual": "0.0", "previous": None}
    with pytest.raises(ValueError, match="精确月率"):
        parse_values("pce", html.split("<table>")[0])


def test_legacy_bls_html_encoding_and_double_dash_heading():
    html = '''<html><body>January 9, 2026 8:30 a.m.<div id="bodytext">
    THE EMPLOYMENT SITUATION -- DECEMBER 2025
    Both total nonfarm payroll employment (+50,000) and the unemployment rate (4.4 percent) changed little.
    Historical note — revised.</div></body></html>'''.encode("cp1252")
    [announcement] = parse_announcements("jobs", html)
    assert announcement.period == "2025-12" and announcement.values["payrolls"]["actual"] == "5"
    assert announcement.at == datetime(2026, 1, 9, 13, 30, tzinfo=UTC)


def test_official_missing_cpi_monthly_is_not_zero_or_two_month_change(trading):
    from pathlib import Path
    from alphapulse.calendar.parsers import parse_values
    from alphapulse.calendar.service import release_rows
    body = (Path(__file__).parent / "fixtures/calendar/cpi.html").read_text()
    # 官方停摆期表格用横线标识单月读数缺失；正文的累计变化不能代替它。
    from parsel import Selector
    sel = Selector(text=body)
    for node in sel.css("#cpi_pressa tr"):
        cells = node.xpath("./th|./td")
        if len(cells) == 9:
            cells[-2].root.text = "-"
            cells[-3].root.text = "-"
    values = parse_values("cpi", sel.get())
    assert values["cpi_mom"]["actual"] is None and values["cpi_mom"]["previous"] is None
    assert values["cpi_yoy"]["actual"] is not None
    now = datetime.now(UTC)
    row = SimpleNamespace(id=1, family="cpi", source="bls", title="CPI", period="2025-11", stage=None,
                          scheduled_at=now, schedule_status="scheduled", first_received_at=now, values=values,
                          last_checked_at=now, error=None, release_url=None, schedule_url="https://www.bls.gov", revision_no=2)
    [mom, _, core, _] = release_rows(row, now)
    assert mom["status"] == core["status"] == "unavailable"
    assert "两个月" in mom["explanation"] and mom["impact"] == "uncertain"


def test_archive_discovery_uses_real_links_and_excludes_state_gdp(trading):
    from alphapulse.calendar.backfill import archive_links
    body = """<a href="/news/2025/pce">Personal Income and Outlays, December 2024</a>
    <a href="/news/2025/state">Gross Domestic Product by State, 2025</a>
    <a href="/news/2024/old">Personal Income and Outlays, November 2024</a>
    <li class="pager__item--next"><a href="?page=1">Next</a></li>"""
    links, following = archive_links("bea", body, "https://www.bea.gov/news/archive", date(2025, 1, 1), date(2025, 12, 31))
    assert links == ["https://www.bea.gov/news/2025/pce"]
    assert following == "https://www.bea.gov/news/archive?page=1"


def test_result_cannot_cross_period_or_stage_and_does_not_change_first_arrival(trading):
    from alphapulse.calendar.service import save_result
    row = SimpleNamespace(period="2025-07", stage="preliminary", values={})
    with pytest.raises(ValueError, match="统计期或发布阶段"):
        save_result(None, row, {}, "url", 1, datetime.now(UTC), period="2025-07", stage="final")
    assert row.values == {}


def test_umich_poll_waits_for_final_instead_of_reusing_preliminary(trading):
    import httpx
    from unittest.mock import AsyncMock
    from alphapulse.calendar.worker import CalendarWorker
    async def check():
        worker = CalendarWorker()
        html = """<h1>Preliminary Results for October 2026</h1><table id="front_table">
        <tr><td></td><td>Oct</td><td>Sep</td></tr><tr><td></td><td>2026</td><td>2026</td></tr>
        <tr><td>Index of Consumer Sentiment</td><td>49.0</td><td>48.1</td></tr></table>"""
        worker.fetch = AsyncMock(return_value=(httpx.Response(200, text=html, request=httpx.Request("GET", "https://www.sca.isr.umich.edu/")), 1))
        row = SimpleNamespace(source="umich", family="umich", period="2026-10", stage="final", scheduled_at=datetime.now(UTC),
                              first_received_at=None)
        try:
            assert await worker.result_page(row) is None
            row.period = "2026-09"
            with pytest.raises(ValueError, match="历史期次"):
                await worker.result_page(row)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("limited_path,status", [("/news.release/cpi_02122025.htm", 429),
                                                ("/news.release/cpi_02122025.htm", 503),
                                                ("/bls/news-release/cpi.htm", 429)])
def test_archive_limit_stops_entire_source_preserves_documents_and_continues_other_source(trading, monkeypatch, limited_path, status):
    import httpx
    from unittest.mock import AsyncMock
    from alphapulse.calendar import backfill

    async def check():
        calls = []
        def respond(request):
            path = request.url.path
            calls.append(path)
            if path == limited_path:
                return httpx.Response(status, headers={"Retry-After": "120"})
            if path == "/bls/news-release/cpi.htm":
                return httpx.Response(200, text=''.join(f'<a href="/news.release/cpi_{d}.htm">CPI</a>' for d in ("01152025", "02122025", "03122025")))
            if path == "/news/archive":
                return httpx.Response(200, text='<a href="/news/2025/pce">Personal Income and Outlays</a>')
            if path in ("/news.release/cpi_01152025.htm", "/news/2025/pce"):
                return httpx.Response(200, text="Original announcement")
            raise AssertionError(f"限流后不应继续请求：{request.url}")
        client = httpx.AsyncClient
        monkeypatch.setattr(backfill.httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)))
        monkeypatch.setattr(backfill, "ensure_quiet_window", AsyncMock())
        # 跳过测试中的请求间隔；实际 HTTP 边界与来源停止行为保持不变。
        monkeypatch.setattr(backfill.asyncio, "sleep", AsyncMock())
        errors = []
        docs = await backfill.fetch_documents(date(2025, 1, 1), date(2025, 12, 31), set(), errors, sources={"bls", "bea"})
        assert [d.source for d in docs] == (["bls", "bea"] if "news.release" in limited_path else ["bea"])
        assert len(errors) == 1 and errors[0]["source"] == "bls"
        assert errors[0]["retry_after"] == "120" and "不完整" in errors[0]["stop_reason"]
        assert limited_path in errors[0]["url"]
        assert "/bls/news-release/ppi.htm" not in calls and "/news.release/cpi_03122025.htm" not in calls
    asyncio.run(check())
