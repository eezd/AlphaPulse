"""欧洲原公告索引。只跟随官网列出的链接，历史错误和缺口交给回补报告。"""
from datetime import date, datetime
import json
from urllib.parse import urlencode, urljoin

from parsel import Selector
import httpx

from alphapulse.calendar.europe import family_for
from alphapulse.calendar.parsers import compact
from alphapulse.core.official_sources import EUROSTAT_FEED, feed_entries

DESTATIS_ARCHIVES = {
    "de_cpi": "https://www.destatis.de/SiteGlobals/Forms/Suche/Presse/EN/Pressesuche_Formular_2.html?cl2Taxonomies_Themen_0=preise&cl2Taxonomies_Themen_1=verbraucherpreisindex",
    "de_gdp": "https://www.destatis.de/SiteGlobals/Forms/Suche/Presse/EN/Pressesuche_Formular_2.html?cl2Taxonomies_Themen_0=volkswirtschaftliche_gesamtrechnungen_inlandsprodukt",
    "de_jobs": "https://www.destatis.de/SiteGlobals/Forms/Suche/Presse/EN/Pressesuche_Formular_2.html?cl2Taxonomies_Themen_0=arbeitsmarkt&cl2Taxonomies_Themen_1=erwerbstaetigkeit",
}


def ons_links(body: str) -> tuple[list[str], str | None]:
    s = Selector(text=body)
    links = []
    for node in s.css("a[data-gtm-release-title]"):
        title = compact(Selector(text=node.attrib["data-gtm-release-title"]).xpath("string(.)").get())
        if family_for("ons", title) and not node.attrib["href"].endswith("timeseries"):
            links.append(urljoin("https://www.ons.gov.uk", node.attrib["href"]))
    following = s.css('a[rel="next"]::attr(href)').get()
    return links, urljoin("https://www.ons.gov.uk", following) if following else None


def destatis_links(body: str, start: date, end: date) -> tuple[list[str], str | None]:
    sections = Selector(text=body).css(".s-press-search-results")
    if not sections:
        raise ValueError("Destatis 历史索引缺少筛选结果列表")
    # 第二块是独立的最新新闻，日期不能参与筛选列表的翻页边界。
    s = sections[0]
    links, days = [], []
    for node in s.css(".c-result"):
        date_text = compact(node.css(".c-result__date").xpath("string(.)").get() or "")
        if " of " not in date_text:
            raise ValueError("Destatis 历史索引缺少公告日期")
        day = datetime.strptime(date_text.rsplit(" of ", 1)[1], "%d %B %Y").date()
        days.append(day)
        a = node.css("h3 a")
        title = compact(a.xpath("string(.)").get())
        if start <= day <= end:
            url = urljoin("https://www.destatis.de/", a.attrib["href"])
            if url.endswith(("_611.html", "_132.html")) or (url.endswith("_811.html") and "quarter" in title):
                links.append(url)
    if not days:
        raise ValueError("Destatis 历史索引没有公告数据行")
    following = s.css("a.forward::attr(href)").get() if max(days) >= start else None
    return links, urljoin("https://www.destatis.de/", following) if following else None


async def collect(source: str, start: date, end: date, fetch, known: set[str], errors: list[dict]):
    """fetch 由回补入口负责限速、只读窗口与官方域名校验。"""
    links = []
    if source == "ecb":
        for year in range(start.year, end.year + 1):
            url = f"https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html"
            doc = await fetch(source, url)
            links.extend(urljoin(url, a) for a in Selector(text=doc.body.decode()).css('a[href]::attr(href)').getall() if "/ecb.mp" in a and a.endswith(".en.html"))
    elif source == "boe":
        url = "https://www.bankofengland.co.uk/sitemap/minutes"
        doc = await fetch(source, url)
        for a in Selector(text=doc.body.decode()).css('a[href]::attr(href)').getall():
            if any(f"/monetary-policy-summary-and-minutes/{y}/" in a for y in range(start.year, end.year + 1)) and not a.endswith(".pdf"):
                links.append(urljoin(url, a))
    elif source == "eurostat":
        for page in range(1, 31):
            url = EUROSTAT_FEED + f"&_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_pageNumber={page}"
            doc = await fetch(source, url)
            entries = feed_entries(doc.body, source)
            if not entries:
                break
            links.extend(e.url for e in entries if start <= e.at.date() <= end and family_for(source, e.title))
            if min(e.at.date() for e in entries) < start:
                break
        else:
            raise ValueError("Eurostat 归档超过已核实的 30 页范围，覆盖未完成")
    elif source == "ons":
        dates = {"after-day": start.day, "after-month": start.month, "after-year": start.year,
                 "before-day": end.day, "before-month": end.month, "before-year": end.year}
        for keyword in ("consumer price inflation", "UK labour market", "GDP quarterly"):
            url = "https://www.ons.gov.uk/releasecalendar?" + urlencode({**dates, "release-type": "type-published", "keywords": keyword, "limit": 100})
            seen = set()
            while url:
                if url in seen:
                    raise ValueError("ONS 归档分页形成循环")
                seen.add(url)
                doc = await fetch(source, url)
                found, url = ons_links(doc.body.decode())
                links.extend(found)
    elif source == "destatis":
        for initial in DESTATIS_ARCHIVES.values():
            url, seen = initial, set()
            while url:
                if url in seen:
                    raise ValueError("Destatis 归档分页形成循环")
                seen.add(url)
                doc = await fetch(source, url)
                found, url = destatis_links(doc.body.decode(), start, end)
                links.extend(found)
    elif source == "insee":
        # 官网 agenda-diffusion 返回的六个统计公报系列 ID；不是当前修订时间序列。
        for family_id in (1250, 137, 1251, 109, 104):
            offset = 0
            while True:
                doc = await fetch(source, "https://www.insee.fr/fr/solr/consultation", query={
                    "q": "*:*", "start": offset, "rows": 100,
                    "filters": [{"field": "familleId", "value": str(family_id)}],
                    "sortFields": [{"field": "dateDiffusion", "order": "desc"}],
                })
                result = json.loads(doc.body)
                if result["status"] != 0 or not result["documents"]:
                    raise ValueError("INSEE 历史公报查询失败或没有目标系列")
                documents = result["documents"]
                for item in documents:
                    day = datetime.fromisoformat(item["dateDiffusion"]).date()
                    if start <= day <= end:
                        links.append(f"https://www.insee.fr/fr/statistiques/{item['id']}")
                offset += len(documents)
                if offset >= result["numFounds"] or min(datetime.fromisoformat(d["dateDiffusion"]).date() for d in documents) < start:
                    break
    else:
        raise ValueError(f"未支持的欧洲归档来源：{source}")
    if not links:
        raise ValueError(f"{source} 官方归档没有识别到目标公告，历史覆盖未核实")
    documents = []
    for url in dict.fromkeys(links):
        if url in known:
            continue
        try:
            doc = await fetch(source, url)
            if source == "ons" and "/releases/" in doc.url:
                sel = Selector(text=doc.body.decode())
                title = compact(sel.css("h1").xpath("string(.)").get() or "")
                target = family_for(source, title)
                if target is None:
                    raise ValueError("ONS 历史发布页不是目标公报")
                patterns = {"uk_cpi": "/bulletins/consumerpriceinflation/", "uk_gdp": "/bulletins/", "uk_jobs": "/bulletins/uklabourmarket/"}
                candidates = list(dict.fromkeys(urljoin(doc.url, u) for u in sel.css('a[href]::attr(href)').getall() if patterns[target[0]] in u))
                if len(candidates) != 1:
                    raise ValueError("ONS 历史发布页未唯一指向目标公报")
                if candidates[0] in known:
                    known.add(url)
                    continue
                doc = await fetch(source, candidates[0])
            documents.append(doc)
            known.update((url, doc.url))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append({"source": source, "url": url, "error": str(exc)})
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                errors.append({"source": source, "error": "官网限流或暂不可用，已停止该来源回补；本次历史覆盖不完整，请稍后重跑。", "retry_after": exc.response.headers.get("Retry-After")})
                break
    return documents
