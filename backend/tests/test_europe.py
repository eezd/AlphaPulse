"""欧洲真实官方样例：数值口径、时区和初终值隔离。"""
from datetime import UTC, date, datetime
import hashlib
from pathlib import Path

import pytest

from alphapulse.calendar.europe import family_for, local_at, parse_announcement, parse_schedule
from alphapulse.core.official_sources import feed_entries, official_url, relevant_news

FIXTURES = Path(__file__).parent / "fixtures" / "europe"


def fixture(name):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize("family,name,period,stage,expected", [
    ("ecb_rate", "ecb_rate", None, None, {"ecb_deposit": "2.5", "ecb_main": "2.65", "ecb_marginal": "2.9"}),
    ("boe_rate", "boe_decision", None, None, {"bank_rate": "3.75"}),
    ("uk_cpi", "ons_cpi", "2026-09", None, {"eu_cpi_yoy": "3.5", "eu_cpi_mom": "0.4", "eu_core_cpi_yoy": "2.8"}),
    ("uk_jobs", "ons_jobs", "2026-08", None, {"uk_unemployment": "4.7"}),
    ("uk_gdp", "ons_gdp", "2026-Q2", "initial", {"gdp_qoq": "0.4", "gdp_yoy": "1.2"}),
    ("uk_gdp", "uk_final", "2026-Q2", "final", {"gdp_qoq": "0.5", "gdp_yoy": None}),
    ("ea_hicp", "ea_cpi", "2026-09", "final", {"hicp_yoy": "3.8", "hicp_mom": "0.6", "core_hicp_yoy": "2.5"}),
    ("ea_hicp", "ea_flash", "2026-09", "flash", {"hicp_yoy": "3.8", "hicp_mom": "0.6", "core_hicp_yoy": "2.5"}),
    ("ea_jobs", "ea_jobs", "2026-08", None, {"ilo_unemployment": "6.4"}),
    ("ea_gdp", "ea_gdp", "2026-Q2", "preliminary", {"gdp_qoq": "0.4", "gdp_yoy": "1"}),
    ("ea_gdp", "ea_gdp_flash", "2026-Q2", "flash", {"gdp_qoq": "0.4", "gdp_yoy": "1"}),
    ("ea_gdp", "ea_gdp_final", "2026-Q2", "final", {"gdp_qoq": "0.6", "gdp_yoy": "1.2"}),
    ("de_cpi", "de_cpi", "2026-09", "final", {"eu_cpi_yoy": "3.3", "eu_cpi_mom": "0.6", "hicp_yoy": "3.3", "hicp_mom": "0.6"}),
    ("de_gdp", "de_gdp", "2026-Q2", "initial", {"gdp_qoq": "0.2", "gdp_yoy": "0.9"}),
    ("de_jobs", "de_jobs", "2026-08", None, {"ilo_unemployment": "4"}),
    ("fr_cpi", "fr_cpi", "2026-09", "final", {"eu_cpi_yoy": "3.1", "eu_cpi_mom": "-0.3", "hicp_yoy": "3.4", "hicp_mom": "-0.3"}),
    ("fr_cpi", "fr_cpiflash", "2026-09", "preliminary", {"eu_cpi_yoy": "3", "eu_cpi_mom": "-0.3", "hicp_yoy": "3.4", "hicp_mom": "-0.4"}),
    ("fr_gdp", "fr_gdp", "2026-Q2", "final", {"gdp_qoq": "0", "gdp_yoy": None}),
    ("fr_gdp", "fr_initial", "2026-Q2", "initial", {"gdp_qoq": "0.2", "gdp_yoy": None}),
    ("fr_jobs", "fr_jobs", "2026-Q2", None, {"ilo_unemployment": "8.3"}),
    ("ecb_rate", "ecb_hold", None, None, {"ecb_deposit": "2", "ecb_main": "2.15", "ecb_marginal": "2.4"}),
    ("uk_cpi", "ons_columns", "2026-06", None, {"eu_cpi_yoy": "2.6", "eu_cpi_mom": "0.1", "eu_core_cpi_yoy": "2.6"}),
    ("uk_cpi", "ons_core_label", "2026-02", None, {"eu_cpi_yoy": "3", "eu_cpi_mom": "0.4", "eu_core_cpi_yoy": "3.2"}),
    ("uk_cpi", "ons_footnote", "2025-05", None, {"eu_cpi_yoy": "3.4", "eu_cpi_mom": "0.2", "eu_core_cpi_yoy": "3.5"}),
    ("uk_gdp", "ons_gdp_old", "2025-Q1", "initial", {"gdp_qoq": "0.7", "gdp_yoy": "1.3"}),
    ("uk_gdp", "ons_gdp_order", "2025-Q4", "final", {"gdp_qoq": "0.1", "gdp_yoy": None}),
    ("fr_cpi", "fr_stable", "2025-02", "preliminary", {"eu_cpi_yoy": "0.8", "eu_cpi_mom": "0", "hicp_yoy": "0.9", "hicp_mom": "0"}),
    ("fr_cpi", "fr_rebound", "2026-02", "preliminary", {"eu_cpi_yoy": "1", "eu_cpi_mom": "0.7", "hicp_yoy": "1.1", "hicp_mom": "0.8"}),
])
def test_official_announcement_contract(family, name, period, stage, expected):
    result = parse_announcement(family, fixture(name + ".html"))
    assert (result.period, result.stage) == (period, stage)
    assert {key: v["actual"] for key, v in result.values.items()} == expected
    for key, actual in expected.items():
        if actual is None:
            assert result.values[key]["unavailable_reason"]
    assert result.at.tzinfo is UTC


def test_monthly_previous_is_previous_month_not_same_month_last_year():
    result = parse_announcement("uk_cpi", fixture("ons_cpi.html"))
    assert result.values["eu_cpi_mom"]["previous"] == "0.5"
    assert result.values["eu_core_cpi_yoy"]["previous"] == "2.6"


@pytest.mark.parametrize("period,expected", [("December 2024 to February 2025", "2025-02"),
                                           ("December 2025 to February 2026", "2026-02"),
                                           ("June to August 2026", "2026-08")])
def test_ons_unemployment_period_uses_end_of_complete_three_month_range(period, expected):
    body = f'<html><body><h1>Labour market overview, UK: April 2026</h1><p>Release date: 21 April 2026</p><p>The UK unemployment rate for people aged 16 years and over was estimated at 4.9% in {period}. This is above estimates of a year ago.</p></body></html>'
    result = parse_announcement("uk_jobs", body)
    assert result.period == expected and result.values["uk_unemployment"]["actual"] == "4.9"


def test_boe_publication_uses_this_article_not_related_article_timestamp():
    result = parse_announcement("boe_rate", fixture("boe_july.html"))
    assert result.at == datetime(2026, 7, 30, 11, tzinfo=UTC)


def test_french_preliminary_and_final_are_distinct_vintages():
    initial = parse_announcement("fr_cpi", fixture("fr_cpiflash.html"))
    final = parse_announcement("fr_cpi", fixture("fr_cpi.html"))
    assert initial.period == final.period
    assert initial.stage != final.stage and initial.at < final.at
    assert initial.values["hicp_mom"]["actual"] == "-0.4"
    assert final.values["hicp_mom"]["actual"] == "-0.3"


@pytest.mark.parametrize("source,suffix,families", [
    ("ecb", "html", {"ecb_rate"}), ("boe", "html", {"boe_rate"}),
    ("ons", "html", {"uk_cpi", "uk_jobs", "uk_gdp"}),
    ("destatis", "html", {"de_cpi", "de_jobs", "de_gdp"}),
    ("eurostat", "json", {"ea_hicp", "ea_gdp", "ea_jobs"}),
    ("insee", "json", {"fr_cpi", "fr_gdp", "fr_jobs"}),
])
def test_calendar_families_and_stable_identity(source, suffix, families):
    events = parse_schedule(source, fixture(f"{source}_schedule.{suffix}"))
    assert {e.family for e in events} == families
    assert len({e.external_id for e in events}) == len(events)
    assert all(e.at.tzinfo is UTC for e in events)


def test_summer_time_and_official_placeholder_calendar_times():
    assert local_at("ecb", date(2026, 9, 10), "14:15") == datetime(2026, 9, 10, 12, 15, tzinfo=UTC)
    assert local_at("ecb", date(2026, 10, 29), "14:15") == datetime(2026, 10, 29, 13, 15, tzinfo=UTC)
    german = parse_schedule("destatis", fixture("destatis_schedule.html"))
    assert next(e for e in german if e.family == "de_cpi").at == datetime(2026, 10, 30, 13, tzinfo=UTC)
    assert next(e for e in german if e.family == "de_gdp").stage == "initial"
    euro = parse_schedule("eurostat", fixture("eurostat_schedule.json"))
    assert euro[0].at.hour == 9


@pytest.mark.parametrize("source", ["ecb", "boe", "eurostat", "ons", "destatis", "insee"])
def test_public_feed_contract(source):
    entries = feed_entries((FIXTURES / f"{source}_feed.xml").read_bytes(), source)
    assert entries and all(e.at.tzinfo is UTC for e in entries)
    assert all(len(e.identity) == 64 for e in entries)
    assert all(official_url(source, e.url) == e.url for e in entries)


def test_source_filters_exclude_wrong_statistical_contracts():
    assert not relevant_news("eurostat", "Government debt at 88.8% of GDP in euro area", "")
    assert family_for("ons", "GDP first quarterly estimate, UK: July to September 2026 time series") is None
    assert family_for("ons", "UK Labour Market: November 2026") == ("uk_jobs", None)
    assert official_url("ecb", "https://www.ecb.europa.eu//press/key/date/a.en.html") == "https://www.ecb.europa.eu/press/key/date/a.en.html"
    with pytest.raises(ValueError, match="官方 HTTPS"):
        official_url("ons", "https://example.com/private")


def test_missing_values_fail_without_zero_fallback():
    with pytest.raises(ValueError):
        parse_announcement("ecb_rate", "<html><body><h1>Monetary policy decisions</h1>Data not available</body></html>")
    with pytest.raises(ValueError):
        feed_entries(b"<html>Access denied</html>", "boe")


def test_history_capture_preserves_raw_bytes_time_language_and_news_identity(trading):
    import base64

    from alphapulse.calendar.backfill import Document
    from alphapulse.crawler.europe_history import capture
    from alphapulse.crawler.import_history import article_item

    body = fixture("de_cpi.html").encode()
    url = "https://www.destatis.de/EN/Press/2026/10/PE26_365_611.html"
    at = datetime(2026, 10, 25, tzinfo=UTC)
    result = capture(Document("destatis", url, body, at, headers={"content-type": "text/html"}))
    assert result.fetched_at == at and result.articles[0].published_at < at
    assert base64.b64decode(result.body) == body
    assert result.headers == {"content-type": "text/html"}
    item = article_item("destatis", result.articles[0], result, 42)
    assert item["extra"]["language"] == "en"  # 英文历史公报不能误标成德文实时 RSS。
    assert item["extra"]["historical_backfill"] is True
    assert item["published_at"] == parse_announcement("de_cpi", body.decode()).at
    assert item["external_id"] == hashlib.sha256(url.encode()).hexdigest()


def test_historical_rate_limit_stops_source_and_preserves_prior_documents():
    import asyncio

    import httpx

    from alphapulse.calendar.backfill import Document
    from alphapulse.calendar.europe_archives import collect

    index = '<a href="/monetary-policy-summary-and-minutes/2025/january-2025">One</a><a href="/monetary-policy-summary-and-minutes/2025/february-2025">Two</a><a href="/monetary-policy-summary-and-minutes/2025/march-2025">Three</a>'
    calls, errors = [], []

    async def fetch(source, url):
        calls.append(url)
        if len(calls) == 3:
            response = httpx.Response(429, headers={"Retry-After": "120"}, request=httpx.Request("GET", url))
            response.raise_for_status()
        return Document(source, url, index.encode(), datetime.now(UTC))

    docs = asyncio.run(collect("boe", date(2025, 1, 1), date(2025, 12, 31), fetch, set(), errors))
    assert len(docs) == 1 and len(calls) == 3
    assert errors[-1]["retry_after"] == "120" and "不完整" in errors[-1]["error"]


def test_ons_release_alias_does_not_download_stored_bulletin_again():
    import asyncio
    from alphapulse.calendar.backfill import Document
    from alphapulse.calendar.europe_archives import collect

    bulletin = "https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/consumerpriceinflation/september2026"
    alias = "https://www.ons.gov.uk/releases/consumerpriceinflationukseptember2026"
    calls, errors, known = [], [], {bulletin}
    async def fetch(source, url):
        calls.append(url)
        if "/releasecalendar?" in url:
            body = f'<a data-gtm-release-title="Consumer price inflation, UK: September 2026" href="{alias}">Release</a>'
        else:
            assert url == alias
            body = f'<h1>Consumer price inflation, UK: September 2026</h1><a href="{bulletin}">Bulletin</a>'
        return Document(source, url, body.encode(), datetime.now(UTC))
    docs = asyncio.run(collect("ons", date(2025, 1, 1), date(2026, 10, 25), fetch, known, errors))
    assert docs == [] and errors == []
    assert bulletin not in calls and calls.count(alias) == 1 and alias in known
