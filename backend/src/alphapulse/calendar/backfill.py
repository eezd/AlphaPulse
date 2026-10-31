"""官方日历历史回补。默认只读预览；--commit 才写库，不启动新闻或交易流程。

uv run python -m alphapulse.calendar.backfill --start 2025-01-01 [--stored-only] [--commit]
按北京时间的公布日期筛选。归档可能已修订，first_received_at 保留真实采集时间，
不能把本次回补用作当时已知信息。失败与无公告缺口会写入 JSON 输出并返回非零状态。
写入时应暂停 calendar 进程，完成后恢复，避免与日程同步并发创建同一事件。
密歇根历史初值/终值取官方年度报告索引与当期 PDF，核对正式日期及日程；
归档尚未公开的报告仍列为缺失，当前月度修订序列不用于填补当期公告。
"""
import argparse
import asyncio
import gzip
import json
import re
import ssl
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from parsel import Selector
from sqlalchemy import select, text

from alphapulse.calendar.archives import Announcement, parse_announcements, parse_umich_report
from alphapulse.calendar import adp, dol, eia, europe
from alphapulse.calendar.catalog import UMICH_CSV, UMICH_HISTORY, UMICH_HOME, UMICH_INFO, UMICH_REPORTS
from alphapulse.calendar.parsers import Scheduled, bea_family, compact
from alphapulse.calendar.service import record_revision, save_raw, save_result, save_schedule, save_series
from alphapulse.calendar.umich import current_result, document_links, monthly_series, parse_schedule, pdf_text, report_links
from alphapulse.core.db import SessionLocal
from alphapulse.core.official_sources import HOSTS as EUROPE_HOSTS
from alphapulse.models import EconomicRelease, RawBody, RawResponse

LOCAL = ZoneInfo("Asia/Shanghai")
HOSTS = {"bls": {"www.bls.gov", "www.dol.gov"}, "bea": {"www.bea.gov"},
         "fed": {"www.federalreserve.gov"}, "umich": {"www.sca.isr.umich.edu", "data.sca.isr.umich.edu"}}
US_SOURCES = frozenset(HOSTS)
HOSTS["dol"] = {"www.dol.gov", "oui.doleta.gov"}
HOSTS["eia"] = {"www.eia.gov", "ir.eia.gov"}
HOSTS["adp"] = {"mediacenter.adp.com"}
HOSTS.update(EUROPE_HOSTS)


@dataclass(frozen=True)
class Document:
    source: str
    url: str
    body: bytes
    captured_at: datetime
    raw_id: int | None = None
    headers: dict[str, str] = field(default_factory=dict)


def family_for(document: Document) -> str | None:
    if document.source in EUROPE_HOSTS:
        paths = {"ecb": r"/press/pr/date/20\d\d/html/ecb\.mp", "boe": r"/monetary-policy-summary-and-minutes/20\d\d/",
                 "ons": r"/bulletins/", "eurostat": r"/(?:product\?|(?:en/)?web/products-euro-indicators/w/)",
                 "destatis": r"/EN/Press/20\d\d/\d\d/PE", "insee": r"/fr/statistiques/\d+$"}
        if not re.search(paths[document.source], document.url):
            return None
        sel = Selector(text=document.body.decode("utf-8"), type="html")
        title = compact(" ".join(sel.css("h1").xpath("string(.)").getall()))
        target = europe.family_for(document.source, title)
        return target[0] if target else None
    if document.source == "bls":
        match = re.search(r"/(cpi|ppi|empsit)[._]", document.url)
        return {"cpi": "cpi", "ppi": "ppi", "empsit": "jobs"}[match[1]] if match else None
    if document.source == "bea":
        sel = Selector(text=document.body.decode("utf-8"), type="html")
        if any(compact(n.xpath("string(.)").get()).startswith("Personal Income and Outlays, Data Update")
               for n in sel.css("h1")):
            return None  # 仅修订数据表的通知不是新的当期公布，不回滚原公告。
        return next((family for n in sel.css("h1") if (family := bea_family(compact(n.xpath("string(.)").get())))), None)
    if document.source == "fed":
        return "fomc" if re.search(rb"Federal Reserve issues FOMC statement", document.body) else None
    return None


def archive_links(source: str, body: str, base: str, start: date, end: date) -> tuple[list[str], str | None]:
    """只跟随官方索引的实际链接，不按月份合成公告地址。"""
    if source == "dol":
        return dol.archive_links(body, start, end), None
    sel = Selector(text=body, type="html")
    links = []
    for node in sel.css("a[href]"):
        url = urljoin(base, node.attrib["href"])
        path = urlparse(url).path
        if source == "bls":
            match = re.search(r"/news.release/(?:archives/)?(?:cpi|ppi|empsit)_(\d{8})\.(?:htm|pdf)$", path)
            if match and start <= datetime.strptime(match[1], "%m%d%Y").date() <= end:
                links.append(url)
        elif source == "bea":
            match = re.search(r"/news/(20\d\d)/", path)
            if match and start.year <= int(match[1]) <= end.year and bea_family(compact(node.xpath("string(.)").get())):
                links.append(url)
        elif source == "fed":
            match = re.search(r"/pressreleases/monetary(\d{8})a\.htm$", path)
            if match and start <= datetime.strptime(match[1], "%Y%m%d").date() <= end:
                links.append(url)
    following = sel.css(".pager__item--next a::attr(href)").get() if source == "bea" else None
    # BEA 按发布时间倒序；整页已早于开始年份后，不再翻阅无关历史。
    years = [int(y) for y in re.findall(r"/news/(20\d\d)/", body)]
    if following and years and max(years) < start.year:
        following = None
    unique = set(links)
    # 同一公告优先使用带完整表格的 HTML；无需重复下载、解析同版 PDF。
    links = [u for u in dict.fromkeys(links) if not u.endswith(".pdf") or u[:-4] + ".htm" not in unique]
    return links, urljoin(base, following) if following else None


async def ensure_quiet_window() -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        imminent = await session.scalar(select(EconomicRelease.id).where(
            EconomicRelease.schedule_status == "scheduled",
            EconomicRelease.scheduled_at.between(now - timedelta(minutes=2), now + timedelta(minutes=2)),
        ).limit(1))
    if imminent is not None:
        raise RuntimeError("当前处于官方公布前后两分钟窗口，请稍后重跑回补")


async def fetch_documents(start: date, end: date, known: set[str], errors: list[dict], *, sources: set[str] | None = None) -> list[Document]:
    documents = []
    selected = sources if sources is not None else US_SOURCES
    stopped_sources = set()

    def report_error(source: str, url: str, exc: Exception) -> None:
        failure = {"source": source, "url": url, "error": str(exc)}
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
            stopped_sources.add(source)
            failure.update(retry_after=exc.response.headers.get("Retry-After"), stop_reason="已停止该来源，历史覆盖不完整。")
        errors.append(failure)

    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client, httpx.AsyncClient(timeout=15, verify=ctx) as bls:
        async def fetch(source: str, url: str, *, query: dict | None = None) -> Document:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in HOSTS[source]:
                raise ValueError("归档链接不是对应机构官方 HTTPS 地址")
            await ensure_quiet_window()
            await asyncio.sleep(1)
            current = url
            for _ in range(6):
                if urlparse(current).scheme != "https" or urlparse(current).hostname not in HOSTS[source]:
                    raise ValueError("归档重定向不属于对应机构官方 HTTPS 地址")
                transport = bls if source == "bls" else client
                response = await transport.post(current, json=query, headers={"User-Agent": "AlphaPulse calendar history"}) if query is not None else await transport.get(current, headers={"User-Agent": "AlphaPulse calendar history"})
                if not response.is_redirect:
                    break
                current = urljoin(current, response.headers["location"])
                if response.status_code == 303:
                    query = None
            else:
                raise ValueError("官方归档重定向超过 5 次")
            response.raise_for_status()
            return Document(source, str(response.url), response.content, datetime.now(UTC), headers=dict(response.headers))

        indexes = [("bls", f"https://www.bls.gov/bls/news-release/{name}.htm") for name in ("cpi", "ppi", "empsit")]
        indexes += [("bea", "https://www.bea.gov/news/archive?field_related_product_target_id=All&created_1=All&title="),
                    ("fed", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")]
        indexes += [("dol", dol.SCHEDULE)]
        for source, initial in indexes:
            if source not in selected or source in stopped_sources:
                continue
            url = initial
            visited = set()
            while url:
                if url in visited:
                    errors.append({"url": url, "error": "官方分页形成循环"})
                    break
                visited.add(url)
                try:
                    index = await fetch(source, url)
                    links, following = archive_links(source, index.body.decode("utf-8"), url, start, end)
                    if not links and url == initial:
                        raise ValueError("官方归档未识别到目标公告，覆盖范围尚未核实")
                    if source == "dol":
                        years = {int(urlparse(link).path.split("/")[2]) for link in links}
                        absent = set(range(start.year, end.year + 1)) - years
                        if absent:
                            errors.append({"source": source, "url": url,
                                           "error": f"DOL 归档索引未列出年份 {sorted(absent)}，历史覆盖不完整"})
                    for link in links:
                        if link in known:
                            continue
                        try:
                            documents.append(await fetch(source, link))
                            known.add(link)
                        except (httpx.HTTPError, ValueError) as exc:
                            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                                raise
                            report_error(source, link, exc)
                    url = following
                except (httpx.HTTPError, ValueError) as exc:
                    report_error(source, str(exc.request.url) if isinstance(exc, httpx.HTTPError) else url, exc)
                    break
        if "dol" in selected and "dol" not in stopped_sources and dol.LATEST not in known:
            try:
                documents.append(await fetch("dol", dol.LATEST))
            except (httpx.HTTPError, ValueError) as exc:
                report_error("dol", dol.LATEST, exc)
        if "adp" in selected:
            url = adp.INDEX
            visited = set()
            while url:
                try:
                    if url in visited:
                        raise ValueError("ADP 官方分页形成循环")
                    visited.add(url)
                    page = await fetch("adp", url)
                    links, following = adp.release_links(page.body.decode(), url, start, end)
                    for link in links:
                        if link not in known:
                            documents.append(await fetch("adp", link))
                            known.add(link)
                    url = following
                except (httpx.HTTPError, ValueError) as exc:
                    report_error("adp", str(exc.request.url) if isinstance(exc, httpx.HTTPError) else url, exc)
                    break
        if "eia" in selected:
            try:
                documents.append(await fetch("eia", eia.SCHEDULE))
                index = await fetch("eia", eia.ARCHIVE)
                for url in eia.archive_links(index.body.decode(), start, end):
                    page = await fetch("eia", url)
                    link = eia.csv_link(page.body.decode(), url)
                    if link not in known:
                        documents.append(await fetch("eia", link))
                        known.add(link)
            except (httpx.HTTPError, ValueError) as exc:
                report_error("eia", str(exc.request.url) if isinstance(exc, httpx.HTTPError) else eia.ARCHIVE, exc)
        from alphapulse.calendar.europe_archives import collect
        for source in sorted(selected & EUROPE_HOSTS.keys()):
            try:
                documents += await collect(source, start, end, fetch, known, errors)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                report_error(source, str(exc.request.url) if isinstance(exc, httpx.HTTPError) else "", exc)
        for url in ((UMICH_HISTORY, UMICH_INFO, UMICH_HOME, UMICH_CSV) if "umich" in selected else ()):
            if "umich" in stopped_sources:
                break
            try:
                doc = await fetch("umich", url)
                documents.append(doc)
                if url in (UMICH_HISTORY, UMICH_INFO):
                    labels = ({"Historical Prelim/Final Release Dates"} if url == UMICH_HISTORY else
                              {f"{year} Release Dates" for year in range(start.year, end.year + 1)})
                    links = document_links(doc.body.decode("utf-8"), url, labels)
                    if not links:
                        raise ValueError("密歇根官方索引缺少日程 PDF")
                    for link in links.values():
                        documents.append(await fetch("umich", link))
            except (httpx.HTTPError, ValueError) as exc:
                report_error("umich", str(exc.request.url) if isinstance(exc, httpx.HTTPError) else url, exc)
        # 公告可能在次月公布（如 2025-07 终值在 8 月 1 日）；保留开始月份的前一期，最终按 PDF 正式日期过滤。
        first_period = (start.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        for year in (range(int(first_period[:4]), end.year + 1) if "umich" in selected else ()):
            if "umich" in stopped_sources:
                break
            url = f"{UMICH_REPORTS}?year={year}"
            try:
                index = await fetch("umich", url)
                documents.append(index)
                links = report_links(index.body.decode("utf-8"), year)
                for (period, _), link in links.items():
                    if first_period <= period <= end.strftime("%Y-%m") and link not in known:
                        try:
                            documents.append(await fetch("umich", link))
                            known.add(link)
                        except (httpx.HTTPError, ValueError) as exc:
                            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                                raise
                            report_error("umich", link, exc)
            except (httpx.HTTPError, ValueError) as exc:
                report_error("umich", str(exc.request.url) if isinstance(exc, httpx.HTTPError) else url, exc)
    return documents


async def save_announcement(session, document: Document, family: str, announcement: Announcement, title: str) -> bool:
    # 官方公告日期＋系列＋期次定位已有 ICS 事件，保留其稳定 ID 与首次采集时间。
    candidates = list(await session.scalars(select(EconomicRelease).where(
        EconomicRelease.source == document.source, EconomicRelease.family == family,
        EconomicRelease.scheduled_at == announcement.at,
    ).with_for_update()))
    candidates = [r for r in candidates if r.period in (None, announcement.period) and r.stage in (None, announcement.stage)]
    if len(candidates) > 1:
        raise ValueError("同日同系列匹配到多个日历事件，未写入")
    raw_id = document.raw_id
    row = candidates[0] if candidates else None
    # 回补不回滚已采集的结果；只为已知事件补充缺失的统计期和发布阶段。
    if row is not None and row.first_received_at is not None:
        if (row.period is not None or announcement.period is None) and (row.stage is not None or announcement.stage is None):
            return False
        if raw_id is None:
            raw_id = await save_raw(session, document.source, document.url, 200, document.headers, document.body, document.captured_at)
        row.period, row.stage = announcement.period, announcement.stage
        row.revision_no += 1
        record_revision(session, row, raw_id)
        return True
    if raw_id is None:
        raw_id = await save_raw(session, document.source, document.url, 200, document.headers, document.body, document.captured_at)
    if row is None:
        row = EconomicRelease(source=document.source, family=family,
                              external_id=f"archive-{family}-{announcement.at:%Y%m%dT%H%M}-{announcement.period}-{announcement.stage}",
                              title=title, period=announcement.period, stage=announcement.stage,
                              scheduled_at=announcement.at, schedule_status="scheduled", schedule_url=document.url,
                              schedule_checked_at=document.captured_at, next_check_at=datetime.now(UTC) + timedelta(days=1),
                              values={}, revision_no=1)
        session.add(row)
        await session.flush()
        record_revision(session, row, raw_id)
    save_result(session, row, announcement.values, document.url, raw_id, document.captured_at,
                period=announcement.period, stage=announcement.stage)
    row.error = None
    return True


async def run(start: date, end: date, *, commit: bool = False, stored_only: bool = False, sources: set[str] | None = None) -> dict:
    begin = datetime.combine(start, time.min, LOCAL).astimezone(UTC)
    finish = datetime.combine(end + timedelta(days=1), time.min, LOCAL).astimezone(UTC)
    errors = []
    selected = sources if sources is not None else US_SOURCES
    async with SessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = (await session.execute(select(RawResponse, RawBody.body).join(RawBody, RawBody.sha256 == RawResponse.body_sha256).where(
            RawResponse.source_code.in_(selected), RawResponse.status == 200,
            (RawResponse.url.contains("/news.release/") | RawResponse.url.contains("/newsroom/economicdata/") |
             RawResponse.url.contains("/news/202") | RawResponse.url.contains("/pressreleases/monetary") |
             RawResponse.source_code.in_(("umich", "dol", "eia", "adp")) | RawResponse.source_code.in_(EUROPE_HOSTS)),
        ).distinct(RawResponse.source_code, RawResponse.url, RawResponse.body_sha256)
            .order_by(RawResponse.source_code, RawResponse.url, RawResponse.body_sha256, RawResponse.id))).all()
        documents = [Document(r.source_code, r.url, gzip.decompress(body), r.fetched_at, r.id, r.headers) for r, body in rows]
        existing = list(await session.scalars(select(EconomicRelease).where(
            EconomicRelease.source.in_(selected), EconomicRelease.scheduled_at >= begin, EconomicRelease.scheduled_at < finish)))
    if not stored_only:
        documents += await fetch_documents(start, end, {d.url for d in documents}, errors, **({"sources": sources} if sources is not None else {}))
    announcements = []
    series_points = []
    eia_schedules = [doc for doc in documents if doc.source == "eia" and doc.url == eia.SCHEDULE]
    eia_schedule = max(eia_schedules, key=lambda doc: doc.captured_at) if eia_schedules else None
    # 先处理历史表，再处理年度更正；重跑的稳定身份不依赖日程 PDF 的文件名。
    parsed_schedules = []
    reports = []
    for document in (d for d in documents if d.source == "umich" and d.body.startswith(b"%PDF")):
        try:
            content = await asyncio.to_thread(pdf_text, document.body)
            if "Index of Consumer Sentiment" in content:
                reports.append((document, await asyncio.to_thread(parse_umich_report, content)))
                continue
            parsed_schedules.append((bool(re.search(r"RELEASE DATES FOR 20\d\d", content)), document,
                                     await asyncio.to_thread(parse_schedule, content)))
        except ValueError as exc:
            errors.append({"url": document.url, "error": str(exc)})
    all_schedules: dict[str, tuple[Scheduled, Document]] = {}
    for _, document, events in sorted(parsed_schedules, key=lambda item: (item[0], item[1].captured_at)):
        for event in events:
            all_schedules[event.external_id] = event, document
    schedules = {identity: item for identity, item in all_schedules.items() if begin <= item[0].at < finish}
    for document, announcement in sorted(reports, key=lambda item: item[0].captured_at):
        if not begin <= announcement.at < finish:
            continue
        identity = f"umich-{announcement.period}-{announcement.stage}"
        if identity not in schedules or schedules[identity][0].at != announcement.at:
            errors.append({"url": document.url, "error": "密歇根历史报告的期次、阶段或正式日期与官方日程不符"})
            continue
        announcements.append((document, "umich", announcement, schedules[identity][0].title))
    for document in sorted(documents, key=lambda d: d.captured_at):
        try:
            if document.source == "adp":
                if "ADP-National-Employment-Report-" in urlparse(document.url).path:
                    announcement = adp.report(document.body.decode())
                    if begin <= announcement.at < finish:
                        announcements.append((document, "adp_jobs", announcement, adp.TITLE))
                continue
            if document.source == "eia":
                if urlparse(document.url).path.endswith("/table1.csv"):
                    if eia_schedule is None:
                        raise ValueError("EIA 缺少已采集的公布规则和节假日例外表")
                    announcement = eia.report(document.body.decode(), eia_schedule.body.decode())
                    path = urlparse(document.url).path
                    date_match = re.search(r"/archive/20\d\d/(\d{4}_\d{2}_\d{2})/", path)
                    if date_match and datetime.strptime(date_match[1], "%Y_%m_%d").date() != announcement.at.astimezone(eia.EASTERN).date():
                        raise ValueError("EIA 报告统计周与归档公布日期不符")
                    if begin <= announcement.at < finish:
                        announcements.append((document, "eia_stocks", announcement, "Weekly Petroleum Status Report"))
                continue
            if document.source == "dol":
                if document.body.startswith(b"%PDF"):
                    parsed = await asyncio.to_thread(dol.report, await asyncio.to_thread(pdf_text, document.body))
                    for family, announcement in parsed.items():
                        if begin <= announcement.at < finish:
                            announcements.append((document, family, announcement, "Unemployment Insurance Weekly Claims"))
                continue
            if document.source == "umich":
                if document.url == UMICH_CSV:
                    series_points.append((document, [(p, v) for p, v in monthly_series(document.body.decode())
                                                     if "2024-12" <= p <= end.strftime("%Y-%m")]))
                elif document.url.rstrip("/") == UMICH_HOME.rstrip("/"):
                    period, stage, values = current_result(document.body.decode())
                    identity = f"umich-{period}-{stage}"
                    if identity not in all_schedules:
                        raise ValueError("密歇根当前期次未找到官方公布日程")
                    event, _ = all_schedules[identity]
                    if begin <= event.at < finish:
                        announcements.append((document, "umich", Announcement(event.at, period, stage, values), event.title))
                continue
            family = family_for(document)
            if family is None:
                continue
            parsed = ([await asyncio.to_thread(europe.parse_announcement, family, document.body.decode())]
                      if document.source in EUROPE_HOSTS else
                      await asyncio.to_thread(parse_announcements, family, document.body, pdf=document.body.startswith(b"%PDF")))
            for announcement in parsed:
                if begin <= announcement.at < finish:
                    title = compact(" ".join(Selector(text=document.body.decode()).css("h1").xpath("string(.)").getall())) if document.source in EUROPE_HOSTS else family
                    announcements.append((document, family, announcement, title))
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append({"url": document.url, "error": f"{type(exc).__name__}: {exc}"})
    changed = 0
    if commit:
        await ensure_quiet_window()
        if eia_schedule is not None and eia_schedule.raw_id is None:
            async with SessionLocal() as session:
                await save_raw(session, "eia", eia_schedule.url, 200, eia_schedule.headers, eia_schedule.body, eia_schedule.captured_at)
                await session.commit()
        batches = {}
        for event, document in schedules.values():
            _, batch = batches.setdefault((document.url, document.captured_at), (document, []))
            batch.append(event)
        for document, events in batches.values():
            async with SessionLocal() as session:
                raw_id = document.raw_id or await save_raw(session, "umich", document.url, 200, {}, document.body, document.captured_at)
                await save_schedule(session, "umich", UMICH_INFO, events, raw_id, document.captured_at, withdraw_missing=False)
                await session.commit()
        for document, family, announcement, title in announcements:
            async with SessionLocal() as session:
                changed += await save_announcement(session, document, family, announcement, title)
                await session.commit()
        for document, points in series_points:
            async with SessionLocal() as session:
                raw_id = document.raw_id or await save_raw(session, "umich", document.url, 200, {}, document.body, document.captured_at)
                await save_series(session, "umich", "umich_sentiment", points, "final", raw_id, document.captured_at)
                await session.commit()
    actual = {(d.source, f, a.at, a.period, a.stage) for d, f, a, _ in announcements}
    for row in existing:
        if row.first_received_at is not None and not any(
                row.source == s and row.family == f and row.scheduled_at == at
                and row.period in (None, period) and row.stage in (None, stage) for s, f, at, period, stage in actual):
            actual.add((row.source, row.family, row.scheduled_at, row.period, row.stage))
    expected = {(r.source, r.family, r.scheduled_at, r.period, r.stage) for r in existing if r.schedule_status != "withdrawn"}
    expected.update(("umich", "umich", e.at, e.period, e.stage) for e, _ in schedules.values())
    missing = [dict(source=s, family=f, at=at.isoformat(), period=p, stage=stage)
               for s, f, at, p, stage in sorted(expected - actual, key=lambda x: (x[2], x[1]))
               if not any(s == x[0] and f == x[1] and at == x[2] and p in (None, x[3]) and stage in (None, x[4]) for x in actual)]
    return {"mode": "commit" if commit else "dry-run", "start": str(start), "end": str(end),
            "sources": sorted(selected),
            "archive_coverage": "未在线核对" if stored_only else "见 errors；不能仅凭已存日程判定完整",
            "documents": len(documents), "announcements": dict(Counter(f for _, f, _, _, _ in actual)),
            "changed_announcements": changed, "umich_schedule_events": len(schedules),
            "current_series_points": len(series_points[-1][1]) if series_points else 0,
            "unavailable_values": [dict(source=d.source, family=f, period=a.period, indicator=k, reason=v["unavailable_reason"])
                                   for d, f, a, _ in announcements for k, v in a.values.items() if v.get("unavailable_reason")],
            "missing_announcements": missing, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(LOCAL).date())
    parser.add_argument("--stored-only", action="store_true", help="仅重放已存官方响应，不联网核对归档覆盖")
    parser.add_argument("--source", action="append", choices=sorted(HOSTS), help="只处理指定来源，可重复；默认保留原美国范围，欧洲须显式指定")
    parser.add_argument("--commit", action="store_true", help="显式写库；运行前须确认目标库并完成迁移")
    args = parser.parse_args()
    if args.start < date(2025, 1, 1) or args.end < args.start or args.end > datetime.now(LOCAL).date():
        parser.error("公布日期范围必须从 2025-01-01 起，结束日期不得晚于今天")
    report = asyncio.run(run(args.start, args.end, commit=args.commit, stored_only=args.stored_only, sources=set(args.source) if args.source else None))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["errors"] or report["missing_announcements"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
