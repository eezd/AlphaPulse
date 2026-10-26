"""欧洲官方日程与公告。数值只取正式公告，当前修订序列不冒充当期结果。"""
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from parsel import Selector

from alphapulse.calendar.archives import Announcement
from alphapulse.calendar.catalog import FAMILIES
from alphapulse.calendar.parsers import MONTHS, Scheduled, compact
from alphapulse.core.official_sources import official_url

SCHEDULES = {
    "ecb": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html",
    "eurostat": "https://ec.europa.eu/eurostat/o/calendars/eventsJson",
    "boe": "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates",
    "ons": "https://www.ons.gov.uk/releasecalendar?release-type=type-upcoming&limit=100",
    "destatis": "https://www.destatis.de/SiteGlobals/Forms/Suche/Termine/EN/Terminsuche_Formular.html",
    "insee": "https://www.insee.fr/fr/agenda-diffusion",
}
INSEE_QUERY = {"q": "*:*", "start": 0, "rows": 100, "filters": [],
               "sortFields": [{"field": "dateEmbargo_dt", "order": "asc"}]}
ZONES = {"ecb": "Europe/Berlin", "eurostat": "Europe/Luxembourg", "boe": "Europe/London",
         "ons": "Europe/London", "destatis": "Europe/Berlin", "insee": "Europe/Paris"}
N = r"[+−–‑-]?\d+(?:[.,]\d+)?"
FRENCH_MONTHS = dict(zip(("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"), range(1, 13), strict=True))
MONTH_NUMBERS = {**{k.lower(): v for k, v in MONTHS.items()}, **FRENCH_MONTHS}


def local_at(source: str, day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), ZoneInfo(ZONES[source])).astimezone(UTC)


def numeric(value: str) -> str:
    value = compact(value).replace(",", ".").replace("−", "-").replace("–", "-").replace("‑", "-").rstrip("% ")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        raise ValueError(f"官方单元格不是十进制数值：{value}")
    return str(Decimal(value).normalize()) if Decimal(value) else "0"


def value(actual: str, previous: str | None = None) -> dict:
    return {"actual": numeric(actual), "previous": numeric(previous) if previous is not None else None}


def match_value(pattern: str, text: str) -> dict:
    match = re.search(pattern, text, re.I)
    if match is None:
        raise ValueError(f"官方公告未识别到数值：{pattern}")
    return value(match[1])


def period_of(text: str, *, quarter: bool = False) -> str:
    text = compact(text).lower()
    if quarter:
        patterns = (r"q([1-4])\s*[/ -]?\s*(20\d\d)", r"([1-4])(?:st|nd|rd|th|er|e)?\.?\s*(?:quarter|quartal|trimestre)(?: of)?\s+(20\d\d)")
        for pattern in patterns:
            if m := re.search(pattern, text):
                return f"{m[2]}-Q{m[1]}"
        m = re.search(r"(first|second|third|fourth|premier|deuxième|troisième|quatrième)\s+(?:quarter|trimestre)(?: of)?\s+(20\d\d)", text)
        if m:
            q = {"first": 1, "second": 2, "third": 3, "fourth": 4, "premier": 1, "deuxième": 2, "troisième": 3, "quatrième": 4}[m[1]]
            return f"{m[2]}-Q{q}"
        # ONS 的季度公告以月份范围命名；末月必须是季度末。
        months = list(re.finditer(r"(" + "|".join(MONTH_NUMBERS) + r")\s+(20\d\d)", text))
        if months and (month := MONTH_NUMBERS[months[-1][1]]) in (3, 6, 9, 12):
            return f"{months[-1][2]}-Q{month // 3}"
    else:
        matches = list(re.finditer(r"(" + "|".join(MONTH_NUMBERS) + r")\s+(20\d\d)", text))
        if matches:
            m = matches[-1]
            return f"{m[2]}-{MONTH_NUMBERS[m[1]]:02d}"
    raise ValueError(f"官方内容未识别到统计期：{text[:160]}")


def family_for(source: str, title: str) -> tuple[str, str | None] | None:
    t = compact(title).casefold()
    if source == "ecb":
        return ("ecb_rate", None) if t == "monetary policy decisions" else None
    if source == "boe":
        return ("boe_rate", None) if "bank rate" in t or "mpc summary and minutes" in t else None
    if source == "eurostat":
        if "inflation" in t or "hicp" in t:
            return "ea_hicp", "flash" if "flash" in t or "expected" in t else "final"
        if "unemployment" in t:
            return "ea_jobs", None
        if re.search(r"\bgdp\b", t) and not any(x in t for x in ("deficit", "debt", "update")):
            return "ea_gdp", "preliminary" if "preliminary" in t else "flash" if "flash" in t else "final"
    if source == "ons":
        if "time series" in t:
            return None
        if t.startswith("consumer price inflation, uk:"):
            return "uk_cpi", None
        if t.startswith(("labour market overview, uk:", "uk labour market:")):
            return "uk_jobs", None
        if t.startswith("gdp first quarterly estimate, uk:"):
            return "uk_gdp", "initial"
        if t.startswith("gdp quarterly national accounts, uk:"):
            return "uk_gdp", "final"
    if source == "destatis":
        t = t.removeprefix("press ")  # 英文公报 h1 包含栏目标签 Press。
        if ("consumer price" in t and "harmonised" not in t[:12]) or "inflation rate" in t:
            return "de_cpi", "preliminary" if "preliminary" in t or "expected" in t else "final"
        if "gross domestic product" in t:
            return "de_gdp", "initial" if "first release" in t else "final"
        if "monthly labour market" in t or t.startswith("employment in "):
            return "de_jobs", None
    if source == "insee":
        if "prix à la consommation" in t:
            return "fr_cpi", "preliminary" if "provisoir" in t else "final"
        if "comptes nationaux trimestriels" in t:
            return "fr_gdp", "initial" if "première estimation" in t else "final"
        if "chômage" in t and ("bit" in t or "taux de chômage" in t):
            return "fr_jobs", None
    return None


def event(family: str, title: str, at: datetime, period: str | None, stage: str | None, *, url: str | None = None) -> Scheduled:
    identity = f"{family}-{period or at.date().isoformat()}-{stage or 'release'}"
    return Scheduled(identity, family, title, at, period=period, stage=stage, release_url=url)


def eurostat_calendar_url(now: datetime) -> str:
    from urllib.parse import urlencode
    zone = ZoneInfo(ZONES["eurostat"])
    start = (now - timedelta(days=45)).astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=90)).astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return SCHEDULES["eurostat"] + "?" + urlencode({"start": start.isoformat(), "end": end.isoformat(), "theme": "", "category": "", "keywords": "", "isEuroindicator": "true", "authorInclude": "", "authorExclude": ""})


def parse_schedule(source: str, body: str) -> list[Scheduled]:
    results = []
    s = Selector(text=body, type="html") if source not in ("eurostat", "insee") else None
    if source == "ecb":
        for node in s.css("dt"):
            title = compact(node.xpath("string(following-sibling::dd[1])").get() or "")
            if "monetary policy meeting" in title and "non-monetary" not in title and "Day 2" in title:
                day = datetime.strptime(compact(node.xpath("string(.)").get()), "%d/%m/%Y").date()
                results.append(event("ecb_rate", title, local_at(source, day, "14:15"), None, None))
    elif source == "boe":
        for table in s.css(".page-content table"):
            heading = compact(table.xpath("string(preceding::h2[1])").get() or "")
            year = re.search(r"20\d\d", heading)
            if not year:
                raise ValueError("英国央行日程缺少年份")
            for tr in table.css("tr"):
                label = compact(tr.css("td").xpath("string(.)").get() or "")
                if not label:
                    continue
                day = datetime.strptime(label + " " + year[0], "%A %d %B %Y").date()
                link = tr.css('a[href*="monetary-policy-summary-and-minutes"]::attr(href)').get()
                url = official_url(source, urljoin(SCHEDULES[source], link)) if link else None
                results.append(event("boe_rate", "Bank Rate decision", local_at(source, day, "12:00"), None, None, url=url))
    elif source == "eurostat":
        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError("Eurostat 日程不是事件列表")
        for item in data:
            target = family_for(source, item["title"])
            if target is None or not any(t["label"] == "Euro indicators release" for t in item["types"]):
                continue
            family, stage = target
            # 官方 allDay 日程的 11:00Z 是日历占位时间。实际公告在卢森堡 11:00 发布，
            # 已核对公告 article:published_time（夏令时 09:00Z），不能按占位 Z 推迟两小时。
            at = local_at(source, date.fromisoformat(item["start"][:10]), "11:00") if item["allDay"] else datetime.fromisoformat(item["start"]).astimezone(UTC)
            results.append(event(family, item["title"], at, period_of(item["period"], quarter=family == "ea_gdp"), stage))
    elif source == "ons":
        for link in s.css("a[data-gtm-release-title]"):
            title = compact(Selector(text=link.attrib["data-gtm-release-title"], type="html").xpath("string(.)").get())
            target = family_for(source, title)
            if target is None:
                continue
            family, stage = target
            day = datetime.strptime(link.attrib["data-gtm-release-date"], "%Y%m%d").date()
            at = local_at(source, day, link.attrib["data-gtm-release-time"])
            period = None if family == "uk_jobs" else period_of(title, quarter=family == "uk_gdp")
            results.append(event(family, title, at, period, stage, url=official_url(source, urljoin(SCHEDULES[source], link.attrib["href"]))))
    elif source == "destatis":
        for node in s.css(".c-result--event-preview"):
            title = compact(node.css("h3").xpath("string(.)").get())
            target = family_for(source, title)
            if target is None:
                continue
            family, stage = target
            details = compact(node.xpath("string(.)").get())
            family, stage = family_for(source, details)
            m = re.search(r"Date of issue\s*:\s*(\d{4}\.\d{2}\.\d{2})", details)
            if m is None:
                raise ValueError("Destatis 日程缺少正式日期")
            day = datetime.strptime(m[1], "%Y.%m.%d").date()
            p = re.search(r"Reporting period\s*:\s*(.*?)\s*Date of issue", details)
            if p is None:
                raise ValueError("Destatis 日程缺少统计期")
            # ICS 明确采用零点占位。CPI 初值为 14:00，其余为官网声明的 08:00。
            clock = "14:00" if family == "de_cpi" and stage == "preliminary" else "08:00"
            results.append(event(family, title, local_at(source, day, clock), period_of(p[1], quarter=family == "de_gdp"), stage))
    elif source == "insee":
        data = json.loads(body)
        if data["status"] != 0:
            raise ValueError("INSEE 官方日程查询失败")
        for item in data["documents"]:
            title = item["famille"].get("facetteConjoncture", {}).get("libelleFr", "")
            target = family_for(source, title)
            if target is None:
                continue
            family, stage = target
            at = datetime.fromisoformat(item["embargo"]).astimezone(UTC)
            if datetime.fromisoformat(item["embargo"]).tzinfo is None:
                raise ValueError("INSEE 日程缺少时区")
            results.append(event(family, title, at, period_of(item["periodeReferenceEn"], quarter=family in ("fr_gdp", "fr_jobs")), stage,
                                 url=f"https://www.insee.fr/fr/statistiques/{item['id']}"))
    else:
        raise ValueError(f"未支持的欧洲日程来源：{source}")
    if not results:
        raise ValueError(f"{source} 官方日程未识别到目标事件")
    return results


def table_rows(s: Selector) -> list[list[str]]:
    return [[compact(c.xpath("string(.)").get()) for c in tr.css("th,td")] for tr in s.css("table tr")]


def row_named(rows: list[list[str]], label: str) -> list[str]:
    matches = [row for row in rows if row and compact(row[0]).casefold() == label.casefold()]
    if len(matches) != 1:
        raise ValueError(f"官方表格行不唯一或缺失：{label}")
    return matches[0]


def eurostat_row(s: Selector, label: str) -> list[str]:
    # Eurostat 的数据标志 e/p 紧贴数值书写；仅在这一来源的表格边界剥离。
    return [re.sub(r"^(" + N + r")\s*[ep]$", r"\1", cell) for cell in row_named(table_rows(s), label)]


def publication(source: str, s: Selector, text: str, family: str, stage: str | None) -> datetime:
    if source == "eurostat":
        at = datetime.fromisoformat(s.css('meta[property="article:published_time"]::attr(content)').get() or "")
        if at.tzinfo is None:
            raise ValueError("Eurostat 公告时间缺少时区")
        return at.astimezone(UTC)
    if source == "boe":
        label = compact(s.css(".published-date").xpath("string(.)").get() or "")
        m = re.search(r"Published on\s+(\d{1,2} [A-Z][a-z]+ 20\d\d)", label)
        if m is None:
            raise ValueError("英国央行公告缺少本文正式发布日期")
        day = datetime.strptime(m[1], "%d %B %Y").date()
        return local_at(source, day, "12:00")
    if source == "insee":
        first = s.css(".date-diffusion").xpath("string(.)").get() or ""
        m = re.search(r"\d{2}/\d{2}/\d{4}", first)
        if m is None:
            raise ValueError("INSEE 公告缺少正式发布日期")
        clock = "07:30" if family == "fr_jobs" or (family == "fr_gdp" and stage == "initial") else "08:45"
        return local_at(source, datetime.strptime(m[0], "%d/%m/%Y").date(), clock)
    pattern = {"ecb": r"Monetary policy decisions\s*(\d{1,2} [A-Z][a-z]+ 20\d\d)",
               "ons": r"Release date:\s*(\d{1,2} [A-Z][a-z]+ 20\d\d)",
               "destatis": r"Press release No\. \d+ of (\d{1,2} [A-Z][a-z]+ 20\d\d)"}[source]
    m = re.search(pattern, text)
    if m is None:
        raise ValueError(f"{source} 公告缺少正式发布日期")
    day = datetime.strptime(m[1], "%d %B %Y").date()
    clock = "14:15" if source == "ecb" else "07:00" if source == "ons" else "14:00" if family == "de_cpi" and stage == "preliminary" else "08:00"
    return local_at(source, day, clock)


def parse_announcement(family: str, body: str) -> Announcement:
    source = FAMILIES[family].source
    s = Selector(text=body, type="html")
    if source == "eurostat":
        # 官方表格用上标 e/p 表示估计/临时数据；阶段另存，不把上标并入数值。
        for node in s.css("table sup"):
            node.root.getparent().remove(node.root)
    for node in s.css("script,style,nav,footer,header"):
        node.root.getparent().remove(node.root)
    text = compact(" ".join(s.xpath("//body//text() | //main//text()").getall()))
    title = compact(" ".join(s.css("h1").xpath("string(.)").getall()))
    rows = table_rows(s)
    stage = None
    period = None
    values = {}
    if family == "ecb_rate":
        m = re.search(r"deposit facility, the main refinancing operations and the marginal lending facility[^.]*?(?:to|at)\s*(" + N + r")%,\s*(" + N + r")% and\s*(" + N + r")%", text)
        if m is None:
            raise ValueError("ECB 公告缺少三项明确政策利率")
        values = {key: value(m[i]) for i, key in enumerate(FAMILIES[family].indicators, 1)}
    elif family == "boe_rate":
        values["bank_rate"] = match_value(r"Bank [Rr]ate (?:maintained at|reduced to|increased to)\s*(" + N + r")%", title)
    elif source == "eurostat":
        heading = compact(" ".join(s.css(".ecl-page-header__info .ecl-label").xpath("string(.)").getall())).lower()
        if not heading:
            raise ValueError("Eurostat 公告缺少统计期/阶段标题")
        stage = ("flash" if "flash estimate" in heading else "final") if family == "ea_hicp" else ("preliminary" if heading.startswith("preliminary flash") else "flash" if heading.startswith("gdp and employment flash") or heading.startswith("flash estimate") else "final") if family == "ea_gdp" else None
        if family == "ea_hicp":
            p = re.search(r"(?:rate was|expected to be)\s*" + N + r"\s*% in ([A-Z][a-z]+ 20\d\d)", text)
            if p is None:
                raise ValueError("Eurostat 通胀公告缺少当期月份")
            period = period_of(p[1])
            row = eurostat_row(s, "All-items HICP")
            values["hicp_yoy"] = value(row[-2], row[-3])
            values["hicp_mom"] = value(row[-1])
            core = eurostat_row(s, "energy, food, alcohol & tobacco")
            values["core_hicp_yoy"] = value(core[-2], core[-3])
        elif family == "ea_gdp":
            p = re.search(r"(?:the )?((?:first|second|third|fourth) quarter of 20\d\d)", heading, re.I)
            if p is None:
                raise ValueError("Eurostat GDP 公告缺少季度")
            period = period_of(p[1], quarter=True)
            table = next((t for t in s.css("table") if "growth rates of gdp in volume" in compact(t.xpath("string(.)").get()).lower()), None)
            if table is None:
                raise ValueError("Eurostat 缺少实际 GDP 增长表，不能使用就业增长表")
            row = eurostat_row(table, "Euro area")
            count = (len(row) - 1) // 2
            if len(row) != 1 + count * 2 or count < 2:
                raise ValueError("Eurostat GDP 表格列口径改变")
            values = {"gdp_qoq": value(row[count], row[count - 1]), "gdp_yoy": value(row[-1], row[-2])}
        else:
            p = re.search(r"In ([A-Z][a-z]+ 20\d\d), the euro area seasonally[- ]adjusted unemployment rate was\s*(" + N + r")%", text)
            if p is None:
                raise ValueError("Eurostat 公告未识别到欧元区季调失业率")
            period = period_of(p[1])
            table = next((t for t in s.css("table") if "Seasonally adjusted unemployment, totals" in compact(t.xpath("string(.)").get())), None)
            if table is None:
                raise ValueError("Eurostat 缺少全体失业率表，不能使用青年或分性别失业率")
            row = eurostat_row(table, "Euro area")
            count = (len(row) - 1) // 2
            values["ilo_unemployment"] = value(row[count], row[count - 1])
    elif source == "ons":
        stage = "initial" if "first quarterly estimate" in title.lower() else "final" if family == "uk_gdp" else None
        if family == "uk_cpi":
            period = period_of(title)
            # ONS 旧模板有断行粘连、脚注和额外空白列；数值四列始终是前/当期年率、前/当期月率。
            cpi_rows = {re.sub(r"\s+|\[\d+\]", "", r[0]): r for r in rows if r}
            all_items = cpi_rows.get("CPIAllitems")
            core = cpi_rows.get("CPIexcfood,energy,alcoholandtobacco(coreCPI)")
            if all_items is None:
                raise ValueError("ONS 表格缺少 CPI All items")
            if core is None:
                raise ValueError("ONS 表格缺少核心 CPI，不能用核心 CPIH 替代")
            values = {"eu_cpi_yoy": value(all_items[-3], all_items[-4]), "eu_core_cpi_yoy": value(core[-3], core[-4])}
            table = next((t for t in s.css("table") if "CPIH, OOH component and CPI index" in compact(t.xpath("string(.)").get())), None)
            if table is None:
                raise ValueError("ONS 公告缺少月率历史表格")
            data = [r for r in table_rows(table) if len(r) == 10 and re.fullmatch(r"[A-Za-z]{3}", r[1])]
            if len(data) < 2:
                raise ValueError("ONS CPI 月率历史行不足")
            values["eu_cpi_mom"] = value(data[-1][7], data[-2][7])
        elif family == "uk_gdp":
            period = period_of(title, quarter=True)
            q = int(period[-1])
            label = f"Q{q} {period[:4]}"
            table = next((t for t in s.css("table") if "headline national accounts indicators" in compact(t.xpath("string(.)").get()).lower()), None)
            if table is None:
                raise ValueError("ONS 公告缺少实际 GDP 季率表")
            data = table_rows(table)
            for r in data:
                if r:
                    r[0] = re.sub(r"^(20\d\d) (Q[1-4])$", r"\2 \1", r[0])
            current = row_named(data, label)
            prev_q = f"Q{q - 1} {period[:4]}" if q > 1 else f"Q4 {int(period[:4]) - 1}"
            previous = row_named(data, prev_q)
            values["gdp_qoq"] = value(current[1], previous[1])
            annual = re.search(r"(?<!Nominal )GDP is estimated to be\s*(" + N + r")% (higher|lower) in Quarter " + str(q) + r" " + period[:4] + r",? compared with the same quarter a year ago", text)
            if annual is not None:
                amount = Decimal(numeric(annual[1]))
                values["gdp_yoy"] = value(str(-abs(amount)) if annual[2] == "lower" else str(amount))
            else:
                annual = re.search(r"(?:Real GDP|(?<!Nominal )GDP) is estimated to have (increased|decreased|fallen) by (" + N + r")%(?: in Quarter " + str(q) + r" " + period[:4] + r")?, compared with the same quarter a year ago", text)
                if annual is not None:
                    amount = Decimal(numeric(annual[2]))
                    values["gdp_yoy"] = value(str(-abs(amount)) if annual[1] in ("decreased", "fallen") else str(amount))
                else:
                    values["gdp_yoy"] = {"actual": None, "previous": None, "unavailable_reason": "官方当期公报未列示季度实际 GDP 同比，不以人均 GDP、名义 GDP 或全年增长代填。"}
        else:
            m = re.search(r"The UK unemployment rate for people aged 16 years and over was estimated at (" + N + r")% in ([^.]+)", text)
            if m is None:
                raise ValueError("ONS 公告缺少三个月 ILO 失业率")
            period = period_of(m[2])
            values["uk_unemployment"] = value(m[1])
    elif source == "destatis":
        if family == "de_cpi":
            stage = "preliminary" if "expected" in title.lower() else "final"
            p = re.search(r"Consumer price index,\s*([A-Z][a-z]+ 20\d\d)", text)
            if p is None:
                raise ValueError("Destatis CPI 缺少统计期")
            period = period_of(p[1])
            for label, prefix in (("Consumer price index", "eu_cpi"), ("Harmonised index of consumer prices", "hicp")):
                pattern = label + r",[^:]+:\s*(" + N + r")% on the same month a year earlier[^%]*?\s(" + N + r")% on the previous month"
                m = re.search(pattern, text)
                if m is None:
                    raise ValueError(f"Destatis 公告缺少 {label} 年率/月率")
                values[prefix + "_yoy"], values[prefix + "_mom"] = value(m[1]), value(m[2])
        elif family == "de_gdp":
            stage = "final" if "detailed" in title.lower() or "detailed results" in text[:2000].lower() else "initial"
            p = re.search(r"Gross domestic product \(GDP\),\s*([1-4]\s*(?:st|nd|rd|th) quarter of 20\d\d)", text)
            if p is None:
                raise ValueError("Destatis GDP 缺少统计季度")
            period = period_of(re.sub(r"([1-4])\s+(st|nd|rd|th)", r"\1\2", p[1]), quarter=True)
            values["gdp_qoq"] = match_value(r"(" + N + r")% on the previous quarter \(price, seasonally and calendar adjusted\)", text)
            values["gdp_yoy"] = match_value(r"(" + N + r")% on the same quarter a year earlier \(price and calendar adjusted\)", text)
        else:
            m = re.search(r"Adjusted unemployment rate at (" + N + r")% in ([A-Z][a-z]+ 20\d\d)", text)
            if m is None:
                raise ValueError("Destatis 公告缺少经季调 ILO 失业率")
            period = period_of(m[2])
            values["ilo_unemployment"] = value(m[1])
    elif source == "insee":
        subtitle = compact(s.css(".sous-titre").xpath("string(.)").get() or "")
        # INSEE 的统计期与阶段在 h1 的副标题，而不是页面下方的下一期时间。
        subtitle = subtitle or title
        stage = "preliminary" if "provisoir" in subtitle else "final" if family == "fr_cpi" else "initial" if "première estimation" in subtitle else "final" if family == "fr_gdp" else None
        period = period_of(subtitle, quarter=family in ("fr_gdp", "fr_jobs"))
        if family == "fr_cpi":
            if stage == "preliminary":
                ipc = next((t for t in s.css("table") if "Glissement annuel" in compact(t.xpath("string(.)").get()) and "Variation mensuelle" in compact(t.xpath("string(.)").get())), None)
                if ipc is None:
                    raise ValueError("INSEE CPI 初值缺少明确的年率/月率表")
                data = [r for r in table_rows(ipc) if r and re.fullmatch(r"\d{4}-\d{2}", r[0])]
                values["eu_cpi_yoy"] = value(data[0][1], data[1][1])
                values["eu_cpi_mom"] = value(data[0][2], data[1][2])
                h = next((r for r in rows if r and r[0].startswith("Ensemble IPCH")), None)
                if h is None:
                    raise ValueError("INSEE CPI 初值缺少 HICP 年率")
                values["hicp_yoy"] = value(h[-1], h[-2])
                m = re.search(r"harmonisé.{0,220}?Sur un mois, il (baisserait|augmenterait|diminuerait|reculerait|rebondirait|se replierait)(?: légèrement)? de (" + N + r")\s*%", text)
                if m is not None:
                    amount = numeric(m[2])
                    values["hicp_mom"] = value(str(-abs(Decimal(amount))) if m[1] in ("baisserait", "diminuerait", "reculerait", "se replierait") else amount)
                elif re.search(r"harmonisé.{0,220}?Sur un mois, il serait stable", text):
                    values["hicp_mom"] = value("0")
                else:
                    raise ValueError("INSEE 初值缺少 HICP 月率")
            else:
                ipc = row_named(rows, "Ensemble")
                h = row_named(rows, "Ensemble IPCH")
                values = {"eu_cpi_mom": value(ipc[-2]), "eu_cpi_yoy": value(ipc[-1]), "hicp_mom": value(h[-2]), "hicp_yoy": value(h[-1])}
                history = next((t for t in s.css("table") if "IPC ISJ IPCH" in compact(" ".join(t.xpath(".//text()").getall()))), None)
                if history is not None:
                    prior = row_named(table_rows(history), (date.fromisoformat(period + "-01") - timedelta(days=1)).strftime("%Y-%m"))
                    values["eu_cpi_yoy"]["previous"] = numeric(prior[1])
                    values["hicp_yoy"]["previous"] = numeric(prior[3])
        elif family == "fr_gdp":
            table = next((t for t in s.css("table") if "Le produit intérieur brut et ses composantes" in compact(t.xpath("string(.)").get())), None)
            if table is None:
                raise ValueError("INSEE GDP 缺少季度表格")
            data = [r for r in table_rows(table) if r and re.fullmatch(r"\d{4}-T[1-4]", r[0])]
            if data[0][0] != period.replace("Q", "T"):
                raise ValueError("INSEE GDP 图表期次与标题不符")
            values["gdp_qoq"] = value(data[0][-1], data[1][-1])
            # 这份公告的年度列是全年增长/年度结转，不是季度同比；不得代填。
            values["gdp_yoy"] = {"actual": None, "previous": None, "unavailable_reason": "官方当期公告未列示季度实际 GDP 同比；年度增长及增长结转不能代替季度同比。"}
        else:
            table = next((t for t in s.css("table") if "Taux de chômage au sens du BIT" in compact(t.xpath("string(.)").get())), None)
            if table is None:
                raise ValueError("INSEE 公告缺少全体 15 岁以上 ILO 失业率")
            data = [r for r in table_rows(table) if r and re.fullmatch(r"\d{4}-T[1-4]", r[0])]
            if data[0][0] != period.replace("Q", "T"):
                raise ValueError("INSEE 失业率图表期次与标题不符")
            values["ilo_unemployment"] = value(data[0][1], data[1][1])
    else:
        raise ValueError(f"未支持的欧洲公告：{family}")
    if set(values) != set(FAMILIES[family].indicators):
        raise ValueError(f"{family} 公告指标不完整")
    return Announcement(publication(source, s, text, family, stage), period, stage, values)
