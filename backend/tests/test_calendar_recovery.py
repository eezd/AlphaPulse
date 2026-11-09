"""Offline regression coverage for official-source recovery and throttling."""
import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.parametrize('source', ['eia', 'dol'])
def test_archive_resolution_uses_discovered_links(app_env, source):
    from alphapulse.calendar.worker import CalendarWorker
    async def run():
        worker = CalendarWorker()
        try:
            archive = ('https://www.eia.gov/petroleum/supply/weekly/archive/2026/2026_10_28/wpsr_2026_10_28.php' if source == 'eia'
                       else 'https://oui.doleta.gov/press/2026/102826.pdf')
            response = lambda body, url: httpx.Response(200, text=body, request=httpx.Request('GET', url))
            worker.index = AsyncMock(return_value=(response(f'<a href="{archive}">Report</a>', archive), 1))
            pages = [(response('<a href="download/table1.csv">CSV</a>', archive), 2),
                     (response('synthetic CSV', archive + '/download/table1.csv'), 3)] if source == 'eia' else [(response('synthetic PDF', archive), 2)]
            worker.fetch = AsyncMock(side_effect=pages)
            page, _ = await worker.archive_page(SimpleNamespace(source=source, scheduled_at=datetime(2026, 10, 28, 14, tzinfo=UTC)))
            canonical = archive.rsplit('/', 1)[0] + '/download/table1.csv' if source == 'eia' else archive
            assert page.extensions['calendar_url'] == canonical
            assert worker.fetch.await_args.args == (source, canonical)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('status', [403, 429, 503])
def test_host_cooldown_blocks_network_but_not_other_hosts(app_env, monkeypatch, status):
    from alphapulse.calendar import worker as module
    async def run():
        worker = module.CalendarWorker()
        session = AsyncMock()
        session.__aenter__.return_value = session
        monkeypatch.setattr(module, 'SessionLocal', lambda: session)
        monkeypatch.setattr(module, 'save_raw', AsyncMock(return_value=1))
        worker.http.get = AsyncMock(side_effect=lambda url: httpx.Response(status if 'dol.gov' in url else 200,
            headers={'Retry-After': '7200'}, request=httpx.Request('GET', url)))
        try:
            await worker.fetch('dol', 'https://www.dol.gov/ui/data.pdf')
            with pytest.raises(module.SourceCooling) as error:
                await worker.fetch('dol', 'https://www.dol.gov/ui/other.pdf')
            assert error.value.retry_at >= datetime.now(UTC) + timedelta(seconds=7100)
            await worker.fetch('dol', 'https://oui.doleta.gov/unemploy/archive.asp')
            assert worker.http.get.await_count == 2
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_adp_follows_weekly_page_to_monthly_and_stops_cycles(app_env):
    from alphapulse.calendar import worker as module, adp
    async def run():
        worker = module.CalendarWorker()
        def page(title, next_link=''):
            return httpx.Response(200, text=f'<div class="wd_item_list"><div class="wd_item"><span class="wd_date">Oct 01, 2026</span><div class="wd_title"><a href="/2026-10-01-ADP-National-Employment-Report-Test">{title}</a></div></div></div>{next_link}', request=httpx.Request('GET', adp.INDEX))
        try:
            worker.index = AsyncMock(side_effect=[(page('Weekly estimate', '<div class="wd_page_next"><a href="?o=9">Next</a></div>'), 1),
                (page('ADP National Employment Report: Private-Sector Employment Increased by 1 Jobs in September'), 2)])
            assert len(await worker.adp_links(date(2026, 9, 1), date(2026, 11, 1))) == 1
            assert worker.index.await_count == 2
            worker.index = AsyncMock(return_value=(page('Weekly estimate', '<div class="wd_page_next"><a href="/press-releases">Next</a></div>'), 1))
            with pytest.raises(ValueError, match='循环'):
                await worker.adp_links(date(2026, 9, 1), date(2026, 11, 1))
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_same_poll_requests_are_coalesced_and_next_poll_refetches(app_env):
    from alphapulse.calendar.worker import CalendarWorker
    async def run():
        worker = CalendarWorker()
        worker._fetch = AsyncMock(return_value=(object(), 7))
        try:
            worker.poll_cache = {}
            pages = await asyncio.gather(*(worker.fetch('dol', 'https://www.dol.gov/ui/data.pdf') for _ in range(5)))
            assert all(page == pages[0] for page in pages)
            assert worker._fetch.await_count == 1
            worker.poll_cache = {}
            await worker.fetch('dol', 'https://www.dol.gov/ui/data.pdf')
            assert worker._fetch.await_count == 2
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('source', ['dol', 'eia'])
def test_historical_rows_with_rolling_url_resolve_archives(app_env, monkeypatch, source):
    from alphapulse.calendar import worker as module, dol, eia
    async def run():
        worker = module.CalendarWorker()
        try:
            worker.archive_page = AsyncMock(side_effect=ValueError('archive requested'))
            worker.fetch = AsyncMock(side_effect=AssertionError('rolling URL must not be fetched'))
            worker.eia_schedule_body = 'already verified'
            row = SimpleNamespace(source=source, scheduled_at=datetime(2020, 1, 1, tzinfo=UTC),
                                  release_url=dol.LATEST if source == 'dol' else eia.LATEST + '?Signature=expired')
            with pytest.raises(ValueError, match='archive requested'):
                await worker.result_page(row)
            worker.archive_page.assert_awaited_once_with(row)
            worker.fetch.assert_not_awaited()
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_missing_retry_after_uses_bounded_exponential_delay(app_env, monkeypatch):
    from alphapulse.calendar import worker as module
    clock = [datetime(2026, 11, 2, tzinfo=UTC)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(module, 'datetime', Clock)
    async def run():
        worker = module.CalendarWorker()
        session = AsyncMock()
        session.__aenter__.return_value = session
        monkeypatch.setattr(module, 'SessionLocal', lambda: session)
        monkeypatch.setattr(module, 'save_raw', AsyncMock(return_value=1))
        worker.http.get = AsyncMock(side_effect=lambda url: httpx.Response(429, request=httpx.Request('GET', url)))
        try:
            for expected in (60, 120, 240, 480, 960, 1920, 3600, 3600):
                await worker.fetch('eurostat', 'https://ec.europa.eu/eurostat/')
                retry = worker.cooldowns['ec.europa.eu']
                assert (retry - clock[0]).total_seconds() == expected
                clock[0] = retry + timedelta(seconds=1)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_adp_publication_is_visible_on_next_poll_even_with_old_cache(app_env):
    from alphapulse.calendar import adp
    from alphapulse.calendar.worker import CalendarWorker
    async def run():
        worker = CalendarWorker()
        def page(title):
            return httpx.Response(200, text=f'<div class="wd_item_list"><div class="wd_item"><span class="wd_date">Nov 02, 2026</span><div class="wd_title"><a href="/2026-11-02-ADP-National-Employment-Report-Test">{title}</a></div></div></div>', request=httpx.Request('GET', adp.INDEX))
        pending = (page('Weekly estimate'), 1)
        published = (page('ADP National Employment Report: Private-Sector Employment Increased by 1 Jobs in October'), 2)
        worker.index_cache['adp', adp.INDEX] = (datetime.now(UTC) + timedelta(hours=1), pending)
        worker.fetch = AsyncMock(side_effect=[pending, published])
        try:
            assert await worker.adp_links(date(2026, 11, 2), date(2026, 11, 2)) == []
            assert len(await worker.adp_links(date(2026, 11, 2), date(2026, 11, 2))) == 1
            assert worker.fetch.await_count == 2
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('source', ['dol', 'eia'])
@pytest.mark.parametrize('outcome', ['match', 'wrong_period', 'unavailable', 'invalid_archive'])
def test_missing_archive_checks_latest_without_accepting_wrong_week(app_env, monkeypatch, source, outcome):
    from alphapulse.calendar import worker as module, dol, eia
    from alphapulse.calendar.archives import ArchiveMissing
    from test_dol import REPORT
    from test_eia import SCHEDULE, CSV
    async def run():
        worker = module.CalendarWorker()
        worker.eia_schedule_body = SCHEDULE
        try:
            at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC) if source == 'dol' else datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
            row = SimpleNamespace(source=source, family='initial_claims' if source == 'dol' else 'eia_stocks',
                scheduled_at=at - timedelta(days=7) if outcome == 'wrong_period' else at,
                release_url=None, first_received_at=None, period='2026-10-23')
            worker.archive_page = AsyncMock(side_effect=ValueError('malformed index') if outcome == 'invalid_archive' else ArchiveMissing('archive missing'))
            url = dol.LATEST if source == 'dol' else eia.LATEST
            response = httpx.Response(403 if outcome == 'unavailable' else 200, text=REPORT if source == 'dol' else CSV, request=httpx.Request('GET', url))
            worker.fetch = AsyncMock(return_value=(response, 7))
            monkeypatch.setattr(module, 'pdf_text', lambda _: REPORT)
            if outcome == 'match':
                page, raw = await worker.result_page(row)
                assert raw == 7
                assert page is response
            elif outcome == 'wrong_period':
                with pytest.raises(ValueError, match='所选日程不符'):
                    await worker.result_page(row)
            elif outcome == 'unavailable':
                with pytest.raises(httpx.HTTPStatusError):
                    await worker.result_page(row)
            else:
                with pytest.raises(ValueError, match='malformed index'):
                    await worker.result_page(row)
                worker.fetch.assert_not_awaited()
                return
            worker.fetch.assert_awaited_once_with(source, url)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_result_retirement_is_persistent_and_cooling_does_not_spend_budget(app_env, monkeypatch):
    from alphapulse.calendar import worker as module

    async def run():
        row = SimpleNamespace(source='umich', collection_failures=0, collection_retired_at=None,
                              scheduled_at=datetime(2026, 10, 9, tzinfo=UTC), first_received_at=None,
                              family='umich', error=None)
        session = AsyncMock()
        session.scalar.return_value = row
        session.__aenter__.return_value = session
        monkeypatch.setattr(module, 'SessionLocal', lambda: session)
        worker = module.CalendarWorker()
        try:
            worker.result_page = AsyncMock(side_effect=module.SourceCooling('example', datetime.now(UTC) + timedelta(hours=2)))
            await worker.poll_one(1)
            assert row.collection_failures == 0
            assert row.next_check_at > datetime.now(UTC) + timedelta(hours=1)
            worker.result_page.side_effect = ValueError('official archive missing')
            for _ in range(3):
                await worker.poll_one(1)
            assert row.collection_failures == 3 and row.collection_retired_at is not None
            calls = worker.result_page.await_count
            await worker.poll_one(1)
            assert worker.result_page.await_count == calls
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_dol_lagging_index_uses_only_expected_official_archive(app_env):
    from alphapulse.calendar.worker import CalendarWorker
    async def run():
        worker = CalendarWorker()
        response = lambda url, body: httpx.Response(200, text=body, request=httpx.Request('GET', url))
        worker.index = AsyncMock(return_value=(response('https://oui.doleta.gov/unemploy/archive.asp', '<a href="/press/2025/010225.pdf">old</a>'), 1))
        worker.fetch = AsyncMock(return_value=(response('https://oui.doleta.gov/press/2026/102926.pdf', 'PDF'), 2))
        try:
            await worker.archive_page(SimpleNamespace(source='dol', scheduled_at=datetime(2026, 10, 29, 12, 30, tzinfo=UTC)))
            worker.fetch.assert_awaited_once_with('dol', 'https://oui.doleta.gov/press/2026/102926.pdf')
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())


def test_restart_honors_persisted_source_pause(app_env, monkeypatch):
    from alphapulse.calendar import worker as module
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalars.return_value = [SimpleNamespace(code='adp', enabled=True, paused_until=datetime.now(UTC) + timedelta(hours=1))]
    session.get.return_value = None
    monkeypatch.setattr(module, 'SessionLocal', lambda: session)
    async def run():
        worker = module.CalendarWorker()
        worker.fetch = AsyncMock(side_effect=httpx.ConnectError('offline'))
        try:
            await worker.schedules()
            assert all(call.args[0] != 'adp' for call in worker.fetch.await_args_list)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())

def test_dol_release_day_prefers_dated_archive_without_rolling_request(app_env, monkeypatch):
    from alphapulse.calendar import worker as module, dol
    from test_dol import REPORT
    class ReleaseDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 29, 14, tzinfo=UTC).astimezone(tz)
    monkeypatch.setattr(module, 'datetime', ReleaseDay)
    monkeypatch.setattr(module, 'pdf_text', lambda _: REPORT)
    async def run():
        worker = module.CalendarWorker()
        row = SimpleNamespace(source='dol', family='initial_claims', scheduled_at=datetime(2026, 10, 29, 12, 30, tzinfo=UTC),
                              release_url=dol.LATEST, first_received_at=None)
        page = httpx.Response(200, content=b'%PDF', request=httpx.Request('GET', 'https://oui.doleta.gov/press/2026/102926.pdf'))
        worker.archive_page = AsyncMock(return_value=(page, 7))
        worker.fetch = AsyncMock(side_effect=AssertionError('rolling host must not be fetched'))
        try:
            assert await worker.result_page(row) == (page, 7)
            worker.archive_page.assert_awaited_once_with(row)
            worker.fetch.assert_not_awaited()
            row.scheduled_at -= timedelta(days=7)
            with pytest.raises(ValueError, match='所选日程不符'):
                await worker.result_page(row)
        finally:
            await worker.http.aclose()
            await worker.bls.aclose()
    asyncio.run(run())
