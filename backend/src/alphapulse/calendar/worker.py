"""独立日历采集进程：日程每小时刷新，公布窗口按 5/15/60 秒检查官方结果。"""
import asyncio
import json
import logging
import ssl
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx
from parsel import Selector
from sqlalchemy import select

from alphapulse.calendar.catalog import SCHEDULE_URLS, UMICH_CSV, UMICH_HISTORY, UMICH_HOME, UMICH_INFO
from alphapulse.calendar import adp, dol, eia, europe
from alphapulse.calendar.parsers import EASTERN, MONTHS, bea_release_link, parse_fed_calendar, parse_ics, parse_values, release_date, bls_periods, release_metadata
from alphapulse.calendar.service import apply_bls_periods, record_revision, next_poll, save_raw, save_result, save_schedule, save_series
from alphapulse.calendar.umich import current_result, document_links, monthly_series, parse_schedule, pdf_text
from alphapulse.core.db import SessionLocal
from alphapulse.core.official_sources import FEEDS, HOSTS, feed_entries
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import EconomicRelease

logger = logging.getLogger("alphapulse.calendar")
STATUS_KEY = f"{KEY_PREFIX}calendar:status"
HISTORY_DAYS = 45


def retry_after_at(value: str | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    try:
        value = value.strip()
        if value.isdecimal():
            return now + timedelta(seconds=int(value))
        at = parsedate_to_datetime(value)
        if at.tzinfo is None:
            raise ValueError("Retry-After 日期缺少时区")
        return at.astimezone(UTC)
    except (ValueError, OverflowError, TypeError) as exc:
        logger.warning("官方 Retry-After 无法解析，沿用既有轮询间隔：%s", exc)
        return None


def fed_month_urls(now: datetime) -> list[str]:
    month = now.astimezone(EASTERN).replace(day=1)
    following = (month + timedelta(days=32)).replace(day=1)
    names = {v: k.lower() for k, v in MONTHS.items()}
    return [f"https://www.federalreserve.gov/newsevents/{d.year}-{names[d.month]}.htm" for d in (month, following)]


class CalendarWorker:
    def __init__(self) -> None:
        # 沿用已验证的 BLS TLS 设置；其余来源保持默认配置。
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT")
        self.http = httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": "AlphaPulse economic calendar"})
        self.bls = httpx.AsyncClient(timeout=8, follow_redirects=True, verify=ctx, headers={"User-Agent": "AlphaPulse economic calendar"})
        self.sources: dict[str, dict] = {}
        self.eia_schedule_body: str | None = None
        self.fetch_slots = asyncio.Semaphore(2)

    async def fetch(self, source: str, url: str, *, query: dict | None = None) -> tuple[httpx.Response, int]:
        # 链接来自公开响应，限制到对应官方域名，避免外部内容扩大访问范围。
        allowed = {"bls": {"www.bls.gov"}, "bea": {"www.bea.gov", "apps.bea.gov"}, "fed": {"www.federalreserve.gov"},
                   "umich": {"www.sca.isr.umich.edu", "data.sca.isr.umich.edu"}, "dol": {"www.dol.gov", "oui.doleta.gov"},
                   "eia": {"www.eia.gov", "ir.eia.gov"}, "adp": {"mediacenter.adp.com"}, **HOSTS}
        if urlparse(url).scheme != "https" or urlparse(url).hostname not in allowed[source]:
            raise ValueError("公告链接不是对应机构的官方 HTTPS 地址")
        async with self.fetch_slots:
            client = self.bls if source == "bls" else self.http
            response = await client.post(url, json=query) if query is not None else await client.get(url)
        if urlparse(str(response.url)).hostname not in allowed[source] or response.url.scheme != "https":
            raise ValueError("官方响应重定向到了不受支持的地址")
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            raw_id = await save_raw(session, source, str(response.url), response.status_code,
                                    {k: v for k, v in response.headers.items() if k in ("content-type", "date", "last-modified", "etag", "retry-after")},
                                    response.content, now, method=response.request.method)
            await session.commit()
        return response, raw_id

    async def schedules(self) -> None:
        now = datetime.now(UTC)
        urls = [(s, u) for s, u in SCHEDULE_URLS.items() if s != "fed"] + [("fed", u) for u in fed_month_urls(now)]
        urls += [("bls", f"https://www.bls.gov/schedule/news_release/{code}.htm") for code in ("cpi", "ppi", "empsit")]
        urls += [("umich", UMICH_INFO), ("umich", UMICH_CSV)]
        urls += list(europe.SCHEDULES.items())
        urls += [("dol", dol.SCHEDULE)]
        urls += [("eia", eia.SCHEDULE)]
        urls += [("adp", adp.INDEX)]
        # 启动时明确各来源尚未就绪；月份滚动后移除不再检查的旧月状态。
        self.sources = {url: self.sources.get(url, {"source": source, "checked_at": now.isoformat(), "error": "尚未同步"})
                        for source, url in urls}
        for source, url in urls:
            retry_at = self.sources[url].get("retry_at")
            if retry_at and datetime.fromisoformat(retry_at) > datetime.now(UTC):
                continue
            try:
                fetch_url = europe.eurostat_calendar_url(now) if source == "eurostat" else url
                response, raw_id = await self.fetch(source, fetch_url, query=europe.INSEE_QUERY if source == "insee" else None)
                response.raise_for_status()
                if source == "adp":
                    links, _ = adp.release_links(response.text, url, datetime.min.date(), now.astimezone(EASTERN).date())
                    if not links:
                        raise ValueError("ADP 官方新闻索引缺少近期月报")
                    report, evidence = await self.fetch(source, links[0])
                    report.raise_for_status()
                    events = adp.schedule(report.text, str(report.url))
                    async with SessionLocal() as session:
                        await save_schedule(session, source, url, events, evidence, datetime.now(UTC), withdraw_missing=False)
                        await session.commit()
                elif source in ("dol", "eia"):
                    events = (dol if source == "dol" else eia).schedule(response.text, now)
                    async with SessionLocal() as session:
                        await save_schedule(session, source, url, events, raw_id, datetime.now(UTC), withdraw_missing=False)
                        await session.commit()
                    if source == "eia":
                        self.eia_schedule_body = response.text
                elif source in europe.SCHEDULES:
                    events = europe.parse_schedule(source, response.text)
                    async with SessionLocal() as session:
                        # 欧洲公开日程是滚动/分页窗口，缺失不等于撤回。
                        await save_schedule(session, source, url, events, raw_id, datetime.now(UTC), withdraw_missing=False)
                        await session.commit()
                elif url == UMICH_INFO:
                    await self.umich_schedule(response.text, now)
                elif url == UMICH_CSV:
                    points = [(p, v) for p, v in monthly_series(response.text) if p >= "2024-12" and p <= now.strftime("%Y-%m")]
                    async with SessionLocal() as session:
                        await save_series(session, "umich", "umich_sentiment", points, "final", raw_id, datetime.now(UTC))
                        await session.commit()
                elif source == "bls" and url.endswith(".htm"):
                    code = url.rsplit("/", 1)[1].split(".")[0]
                    async with SessionLocal() as session:
                        await apply_bls_periods(session, "jobs" if code == "empsit" else code, bls_periods(response.text), raw_id)
                        await session.commit()
                else:
                    events = parse_fed_calendar(response.text) if source == "fed" else parse_ics(response.text, source)
                    async with SessionLocal() as session:
                        await save_schedule(session, source, url, events, raw_id, datetime.now(UTC))
                        await session.commit()
                self.sources[url] = {"source": source, "checked_at": datetime.now(UTC).isoformat(), "error": None}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
                self.sources[url] = {"source": source, "checked_at": datetime.now(UTC).isoformat(), "error": str(exc)}
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    retry_at = retry_after_at(exc.response.headers.get("Retry-After"), datetime.now(UTC))
                    if retry_at is not None:
                        self.sources[url]["retry_at"] = retry_at.isoformat()
                logger.exception("日程采集失败：%s", url)

    async def umich_schedule(self, info: str, now: datetime) -> None:
        history, _ = await self.fetch("umich", UMICH_HISTORY)
        history.raise_for_status()
        links = document_links(history.text, UMICH_HISTORY, {"Historical Prelim/Final Release Dates"})
        if "Historical Prelim/Final Release Dates" not in links:
            raise ValueError("密歇根官方资料缺少历史发布日期 PDF")
        documents = list(links.values())
        yearly = document_links(info, UMICH_INFO, {f"{year} Release Dates" for year in (now.year, now.year + 1)})
        if f"{now.year} Release Dates" not in yearly:
            raise ValueError("密歇根官方资料缺少当年日程 PDF")
        documents.extend(yearly.values())
        events = {}
        raw_id = None
        for url in documents:
            response, raw_id = await self.fetch("umich", url)
            response.raise_for_status()
            parsed = await asyncio.to_thread(parse_schedule, await asyncio.to_thread(pdf_text, response.content))
            # 年度日程在历史表之后处理，以官方最新年度安排为准；同一稳定身份不创建重复事件。
            for event in parsed:
                events[event.external_id] = (event, raw_id)
        async with SessionLocal() as session:
            # 不按单份 PDF 撤回其他年度事件；合并日程一次同步，修订引用对应 PDF。
            existing = {r.external_id: r for r in await session.scalars(select(EconomicRelease).where(
                EconomicRelease.source == "umich"))}
            batches = {}
            for event, evidence in events.values():
                if event.at < datetime(2024, 12, 31, 16, tzinfo=UTC):
                    continue
                # 每份 PDF 是部分日程，不能让 save_schedule 的撤回逻辑作用于其他事件。
                batches.setdefault(evidence, []).append(event)
            for evidence, batch in batches.items():
                await save_schedule(session, "umich", UMICH_INFO, batch, evidence, now, withdraw_missing=False)
            if raw_id is not None:
                for row in existing.values():
                    if row.external_id not in events and row.scheduled_at > now and row.first_received_at is None and row.schedule_status != "withdrawn":
                        row.schedule_status = "withdrawn"
                        row.revision_no += 1
                        record_revision(session, row, raw_id)
            await session.commit()

    async def result_page(self, row: EconomicRelease) -> tuple[httpx.Response, int] | None:
        if row.source == "adp":
            if row.release_url:
                url = row.release_url
            else:
                index, _ = await self.fetch("adp", adp.INDEX)
                index.raise_for_status()
                links, _ = adp.release_links(index.text, adp.INDEX, row.scheduled_at.astimezone(EASTERN).date(), row.scheduled_at.astimezone(EASTERN).date())
                if not links:
                    return None
                if len(links) != 1:
                    raise ValueError("ADP 同日月度公告不唯一")
                url = links[0]
            response, raw_id = await self.fetch("adp", url)
            response.raise_for_status()
            announcement = adp.report(response.text)
            if announcement.at != row.scheduled_at or announcement.period != row.period:
                raise ValueError("ADP 公告时间或统计月与所选日程不符")
            return response, raw_id
        if row.source == "eia":
            if self.eia_schedule_body is None:
                schedule, _ = await self.fetch("eia", eia.SCHEDULE)
                schedule.raise_for_status()
                eia.exceptions(schedule.text)
                self.eia_schedule_body = schedule.text
            response, raw_id = await self.fetch("eia", row.release_url or eia.LATEST)
            response.raise_for_status()
            announcement = eia.report(response.text, self.eia_schedule_body)
            if announcement.at < row.scheduled_at or (announcement.at > row.scheduled_at and row.first_received_at is not None):
                return None
            if announcement.at != row.scheduled_at or announcement.period != row.period:
                raise ValueError("EIA 公告统计周或时间与所选日程不符，未填入其他期次的数据")
            return response, raw_id
        if row.source == "dol":
            response, raw_id = await self.fetch("dol", row.release_url or dol.LATEST)
            response.raise_for_status()
            announcements = await asyncio.to_thread(dol.report, await asyncio.to_thread(pdf_text, response.content))
            announcement = announcements[row.family]
            if announcement.at < row.scheduled_at or (announcement.at > row.scheduled_at and row.first_received_at is not None):
                return None
            if announcement.at != row.scheduled_at:
                raise ValueError("DOL 公告时间与所选日程不符，未填入其他期次的数据")
            return response, raw_id
        if row.source in europe.SCHEDULES:
            return await self.european_result(row)
        date = row.scheduled_at.astimezone(EASTERN).date()
        if row.source == "umich":
            response, raw_id = await self.fetch("umich", UMICH_HOME)
            response.raise_for_status()
            period, stage, _ = current_result(response.text)
            if period == row.period and stage == row.stage:
                return response, raw_id
            if period < row.period or (period == row.period and stage == "preliminary" and row.stage == "final"):
                return None
            if row.first_received_at is not None:
                return None  # 最新页已换期；保留已取得的当期值，不误报历史缺口。
            raise ValueError("密歇根当前页已晚于所选历史期次，未取得当期公告")
        if row.source == "bls":
            code = "empsit" if row.family == "jobs" else row.family
            # 本机 BLS archive 路径返回 403，最新公告页可访问；下面必须核对正式发布日期。
            url = f"https://www.bls.gov/news.release/{code}.nr0.htm"
        elif row.source == "fed":
            url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{date:%Y%m%d}a.htm"
        else:
            # 现场核验 BEA RSS 漏了已发布的 PCE；使用官方日程的公告链接，不依赖 RSS 推送时效。
            url = row.release_url
            if url is None:
                schedule, _ = await self.fetch("bea", "https://www.bea.gov/news/schedule/full")
                schedule.raise_for_status()
                url = bea_release_link(schedule.text, date, row.family)
                if url is None:
                    return None
        response, raw_id = await self.fetch(row.source, url)
        if response.status_code == 404:
            return None  # 预约地址尚未发布；保留原始响应，不宣称采集成功。
        response.raise_for_status()
        published = release_date(response.text).date()
        if row.source == "bls" and published < date:
            return None  # 到点后官网仍缓存上一期，等待当期公告，不填旧值。
        if row.source == "bls" and published > date and row.first_received_at is not None:
            return None  # 滚动最新页不再包含本期，不能据此否认已取得的结果。
        if published != date:
            raise ValueError("官方公告日期与日历不符，历史公布值尚未取得")
        return response, raw_id

    async def european_result(self, row: EconomicRelease) -> tuple[httpx.Response, int] | None:
        day = row.scheduled_at.astimezone(europe.ZoneInfo(europe.ZONES[row.source])).date()
        url = row.release_url
        if url is None and row.source == "ecb":
            index, _ = await self.fetch("ecb", f"https://www.ecb.europa.eu/press/govcdec/mopo/{day.year}/html/index_include.en.html")
            index.raise_for_status()
            sel = Selector(text=index.text)
            urls = [urljoin(str(index.url), u) for u in sel.css('a[href]::attr(href)').getall()
                    if f"ecb.mp{day:%y%m%d}~" in u and u.endswith(".en.html")]
            url = next(iter(dict.fromkeys(urls)), None)
        elif url is None and row.source == "destatis":
            # 官方主题页在 RSS 滚出后仍保留最近公告，且提供英文公告的真实链接。
            paths = {"de_cpi": "Economy/Prices/Consumer-Price-Index", "de_gdp": "Economy/National-Accounts-Domestic-Product", "de_jobs": "Labour/Labour-Market/Employment"}
            index, _ = await self.fetch("destatis", f"https://www.destatis.de/EN/Themes/{paths[row.family]}/_node.html")
            index.raise_for_status()
            suffix = {"de_cpi": "_611.html", "de_gdp": "_811.html", "de_jobs": "_132.html"}[row.family]
            candidates = [urljoin("https://www.destatis.de/", u) for u in Selector(text=index.text).css('a[href]::attr(href)').getall()
                          if "/Press/" in u and "/PE" in u and u.endswith(suffix)]
            for candidate in dict.fromkeys(candidates):
                response, raw_id = await self.fetch(row.source, candidate)
                response.raise_for_status()
                announcement = europe.parse_announcement(row.family, response.text)
                if announcement.at.date() == row.scheduled_at.date() and announcement.period == row.period and announcement.stage == row.stage:
                    return response, raw_id
            return None
        elif url is None:
            entries = []
            for feed in FEEDS[row.source]:
                response, _ = await self.fetch(row.source, feed)
                response.raise_for_status()
                entries.extend(feed_entries(response.content, row.source))
            for entry in entries:
                target = europe.family_for(row.source, entry.title)
                if target and target[0] == row.family and entry.at.astimezone(europe.ZoneInfo(europe.ZONES[row.source])).date() == day:
                    # 只有当期同日同系列才取原文；最终还要核对公告的统计期和阶段。
                    url = entry.url
                    break
        if url is None:
            return None
        response, raw_id = await self.fetch(row.source, url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if row.source == "ons" and "/releases/" in str(response.url):
            patterns = {"uk_cpi": "/bulletins/consumerpriceinflation/", "uk_gdp": "/bulletins/gdp", "uk_jobs": "/bulletins/uklabourmarket/"}
            matches = [urljoin(str(response.url), u) for u in Selector(text=response.text).css('a[href]::attr(href)').getall() if patterns[row.family] in u]
            unique = list(dict.fromkeys(matches))
            if len(unique) != 1:
                raise ValueError("ONS 发布页未唯一指向目标统计公报")
            response, raw_id = await self.fetch(row.source, unique[0])
            response.raise_for_status()
        announcement = europe.parse_announcement(row.family, response.text)
        if announcement.at.astimezone(europe.ZoneInfo(europe.ZONES[row.source])).date() != day:
            raise ValueError("欧洲官方公告日期与所选日程不符，未填入其他期次的数据")
        return response, raw_id

    async def poll_one(self, release_id: int) -> None:
        async with SessionLocal() as session:
            row = await session.scalar(select(EconomicRelease).where(EconomicRelease.id == release_id).with_for_update())
            now = datetime.now(UTC)
            retry_at = None
            try:
                page = await self.result_page(row)
                if page is not None:
                    response, raw_id = page
                    if row.source == "adp":
                        announcement = adp.report(response.text)
                        period, stage, values = announcement.period, announcement.stage, announcement.values
                    elif row.source == "eia":
                        announcement = eia.report(response.text, self.eia_schedule_body)
                        period, stage, values = announcement.period, announcement.stage, announcement.values
                    elif row.source == "dol":
                        announcement = (await asyncio.to_thread(dol.report, await asyncio.to_thread(pdf_text, response.content)))[row.family]
                        period, stage, values = announcement.period, announcement.stage, announcement.values
                    elif row.source in europe.SCHEDULES:
                        announcement = europe.parse_announcement(row.family, response.text)
                        period, stage, values = announcement.period, announcement.stage, announcement.values
                    elif row.family == "umich":
                        period, stage, values = current_result(response.text)
                    else:
                        values = parse_values(row.family, response.text)
                        text = " ".join(Selector(text=response.text).css("h1,#bodytext").xpath("string(.)").getall())
                        period, stage = release_metadata(row.family, text)
                    save_result(session, row, values, str(response.url), raw_id, datetime.now(UTC), period=period, stage=stage)
                row.error = None
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
                row.error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    retry_at = retry_after_at(exc.response.headers.get("Retry-After"), datetime.now(UTC))
                logger.exception("公布值采集失败：%s %s", row.family, row.scheduled_at)
            row.last_checked_at = now
            row.next_check_at = next_poll(row.scheduled_at, datetime.now(UTC), row.first_received_at is not None, bool(row.error))
            if retry_at is not None:
                row.next_check_at = max(row.next_check_at, retry_at)
            await session.commit()

    async def results(self) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            ids = list(await session.scalars(select(EconomicRelease.id).where(
                EconomicRelease.source != "jin10",
                EconomicRelease.schedule_status == "scheduled", EconomicRelease.scheduled_at <= now,
                EconomicRelease.scheduled_at >= now - timedelta(days=HISTORY_DAYS), EconomicRelease.next_check_at <= now,
            ).order_by(EconomicRelease.scheduled_at.desc()).limit(8)))
        # 少量同时发布的不同报告独立获取，历史修订不阻塞当期公布。
        await asyncio.gather(*(self.poll_one(id_) for id_ in ids))

    async def run(self) -> None:
        async def schedules_loop() -> None:
            while True:
                await self.schedules()
                await asyncio.sleep(3600)

        async def results_loop() -> None:
            while True:
                await self.results()
                await asyncio.sleep(1)

        async def heartbeat_loop() -> None:
            while True:
                await redis.set(STATUS_KEY, json.dumps({"at": datetime.now(UTC).isoformat(), "sources": self.sources}), ex=30)
                await asyncio.sleep(5)

        try:
            async with asyncio.TaskGroup() as tasks:
                for loop in (schedules_loop, results_loop, heartbeat_loop):
                    tasks.create_task(loop())
        finally:
            await self.http.aclose()
            await self.bls.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(CalendarWorker().run())
