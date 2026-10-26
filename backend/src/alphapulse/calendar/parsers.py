"""官方来源的窄口径解析；页面改变或数据缺失时报错，不返回伪造的零值。"""
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from parsel import Selector

EASTERN = ZoneInfo("America/New_York")
MONTHS = {name: i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}


@dataclass(frozen=True)
class Scheduled:
    external_id: str
    family: str
    title: str
    at: datetime
    status: str = "scheduled"
    period: str | None = None
    stage: str | None = None
    release_url: str | None = None


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def bea_family(title: str) -> str | None:
    if title.startswith("Personal Income and Outlays"):
        return "pce"
    return "gdp" if re.match(r"(?:Gross Domestic Product|GDP)\s*[(,]", title) else None


def bea_release_link(body: str, day: date, family: str) -> str | None:
    sel = Selector(text=body, type="html")
    rows = sel.css("tr.scheduled-releases-type-press")
    if not rows:
        raise ValueError("BEA 公告日程缺少数据行")
    matches = []
    for row in rows:
        title = compact(row.css(".release-title").xpath("string(.)").get())
        date_text = compact(row.css(".release-date").xpath("string(.)").get())
        if date_text != day.strftime("%B ") + str(day.day) or bea_family(title) != family:
            continue
        link = row.css('a[href*="/news/"]::attr(href)').get()
        if link and f"/news/{day.year}/" in link:
            matches.append(urljoin("https://www.bea.gov", link))
    if len(matches) > 1:
        raise ValueError("BEA 同日同类公告不唯一，不能确定结果")
    return matches[0] if matches else None


def parse_ics(body: str, source: str) -> list[Scheduled]:
    body = re.sub(r"\r?\n[ \t]", "", body)
    if "BEGIN:VCALENDAR" not in body or "END:VCALENDAR" not in body:
        raise ValueError("官方日历不是完整 iCalendar")
    results = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", body, re.S):
        fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
        title = fields["SUMMARY"].replace(r"\,", ",").replace(r"\n", " ")
        family = {"Consumer Price Index": "cpi", "Producer Price Index": "ppi", "Employment Situation": "jobs"}.get(title)
        if source == "bea":
            family = bea_family(title)
        if family is None:
            continue
        key, value = next((k, v) for k, v in fields.items() if k.split(";")[0] == "DTSTART")
        if value.endswith("Z"):
            at = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        elif "TZID=US-Eastern" in key or "TZID=America/New_York" in key:
            at = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=EASTERN).astimezone(UTC)
        else:
            raise ValueError(f"日历缺少受支持的精确时区：{key}")
        status = fields.get("STATUS", "CONFIRMED")
        if status not in ("CONFIRMED", "TENTATIVE", "CANCELLED"):
            raise ValueError(f"不支持的日历状态：{status}")
        period, stage = release_metadata(family, title)
        results.append(Scheduled(fields["UID"], family, title, at, "withdrawn" if status == "CANCELLED" else "scheduled", period, stage))
    if not results:
        raise ValueError("官方日历未找到美国目标指标")
    return results


def parse_fed_calendar(body: str) -> list[Scheduled]:
    sel = Selector(text=body, type="html")
    heading = next((compact(n.xpath("string(.)").get()) for n in sel.css("h2,h3,h4")
                    if re.fullmatch(r"[A-Za-z]+ 20\d\d", compact(n.xpath("string(.)").get()))), None)
    if heading is None:
        raise ValueError("美联储月历缺少年月")
    month, year = heading.split()
    results = []
    for node in sel.xpath('//p[normalize-space(.)="FOMC Meeting"]'):
        row = node.xpath("../..")
        clock = compact(row.css(".col-xs-2").xpath("string(.)").get()).replace("a.m.", "AM").replace("p.m.", "PM")
        day = int(compact(row.css(".col-xs-3").xpath("string(.)").get()))
        time = datetime.strptime(clock, "%I:%M %p")
        at = datetime(int(year), MONTHS[month], day, time.hour, time.minute, tzinfo=EASTERN).astimezone(UTC)
        results.append(Scheduled(f"fomc-{year}-{MONTHS[month]:02d}-{day:02d}", "fomc", "FOMC 利率决议", at))
    # 官方月份无会议是正常状态；无法识别页面结构则不能解释成无会议。
    if not sel.css(".cal-nojs__rowTitle"):
        raise ValueError("美联储月历结构无法识别")
    return results


def release_date(body: str) -> datetime:
    text = compact(Selector(text=body, type="html").xpath("string(//body)").get())
    match = re.search(r"(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, 20\d{2}", text)
    if match is None:
        raise ValueError("公告缺少正式发布日期")
    return datetime.strptime(match[0], "%B %d, %Y").replace(tzinfo=EASTERN)


def number(value: str) -> str:
    cleaned = value.strip().replace(",", "").replace("−", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
        raise ValueError(f"公布值不是有效数字：{value!r}")
    return str(Decimal(cleaned))


def cells(row: Selector) -> list[str]:
    return [compact(n.xpath("string(.)").get()) for n in row.xpath("./th|./td")]


def release_metadata(family: str, title: str) -> tuple[str | None, str | None]:
    """只解析来源明示的统计期；不以发布时间推算上月或上季。"""
    if family == "gdp":
        quarter = re.search(r"(1st|2nd|3rd|4th|first|second|third|fourth)\s+quarter(?:\s+and\s+year|\s+of)?\s+(20\d\d)", title, re.I)
        stages = re.search(r"\b(advance|second|third|initial|updated)\s+estimate\b", title, re.I)
        numbers = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "first": 1, "second": 2, "third": 3, "fourth": 4}
        return (f"{quarter[2]}-Q{numbers[quarter[1].lower()]}" if quarter else None,
                stages[1].lower() if stages else None)
    month = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(20\d\d)\b", title, re.I)
    return (f"{month[2]}-{MONTHS[month[1].title()]:02d}" if month else None, None)


def bls_periods(body: str) -> dict[datetime, str]:
    sel = Selector(text=body, type="html")
    periods = {}
    for node in sel.css("table tr"):
        row = cells(node)
        if len(row) != 3 or not re.fullmatch(r"[A-Za-z]+ 20\d\d", row[0]):
            continue
        period, _ = release_metadata("cpi", row[0])
        day = row[1].replace(".", "")
        at = datetime.strptime(day + " " + row[2], "%b %d, %Y %I:%M %p").replace(tzinfo=EASTERN).astimezone(UTC)
        periods[at] = period
    if not periods:
        raise ValueError("BLS 分项日程缺少统计月份")
    return periods


def parse_values(family: str, body: str) -> dict:
    sel = Selector(text=body, type="html")
    content = sel.css("#bodytext,.field--name-body,#article")
    if not content:
        raise ValueError("官方公告缺少正文")
    text = compact(content.xpath("string(.)").get()).replace("‑", "-")
    values = {}

    def put(key: str, actual: str, previous: str | None = None) -> None:
        values[key] = {"actual": number(actual), "previous": number(previous) if previous is not None else None}

    if family == "cpi":
        rows = {r[0]: r[1:] for n in sel.css("#cpi_pressa tr") if len(r := cells(n)) == 9}
        for label, prefix in [("All items", "cpi"), ("All items less food and energy", "core_cpi")]:
            row = rows[label]
            previous = None if row[-3] == "-" else row[-3]
            if row[-2] == "-":
                values[prefix + "_mom"] = {"actual": None, "previous": number(previous) if previous is not None else None,
                                           "unavailable_reason": "官方表格未发布该期单月读数；不能用两个月累计变化替代月率。"}
            else:
                put(prefix + "_mom", row[-2], previous)
            put(prefix + "_yoy", row[-1])
    elif family == "ppi":
        rows = [r for n in sel.css("#ppi_nrtablea tr") if len(r := cells(n)) == 13 and re.match(r"[A-Z][a-z]", r[0])]
        if len(rows) < 2:
            raise ValueError("PPI 缺少本期与前期数据")
        put("ppi_mom", rows[-1][1], rows[-2][1])
        put("ppi_yoy", rows[-1][-2], rows[-2][-2])
    elif family == "jobs":
        employment = re.search(r"nonfarm payroll employment (?:(?:changed little(?: in [A-Za-z]+)?\s*)?\(([+-]?[\d,]+)\)|(?:rose|increased|declined|decreased|fell|edged up|edged down)(?: by)? ([\d,]+))", text)
        if employment is None:
            raise ValueError("就业报告缺少非农新增值")
        actual = Decimal(number(employment[1] or employment[2]))
        if re.search(r"declined|decreased|fell|edged down", employment[0]):
            actual = -actual
        revised = re.findall(r"change for \w+ was revised (?:up|down) by [\d,]+, from [+-]?[\d,]+ to ([+-]?[\d,]+)", text)
        previous = str(Decimal(number(revised[-1])) / 10000) if revised else None
        put("payrolls", str(actual / 10000), previous)
        rate = re.search(r"unemployment rate(?:, at| \(| (?:was unchanged|was|remained|held|changed little)(?: at)?| (?:rose|increased|fell|declined|decreased|edged down|edged up) to)\s*(\d+(?:\.\d+)?) percent", text)
        if rate is None:
            raise ValueError("就业报告缺少失业率")
        put("unemployment", rate[1])
    elif family == "pce":
        rows = {r[0].replace("index,", "index"): r[1:] for n in sel.css("table tr") if len(r := cells(n)) in (2, 3)}
        for label, key in [("PCE price index", "pce_mom"), ("PCE price index excluding food and energy", "core_pce_mom")]:
            if label in rows:
                put(key, rows[label][-1], rows[label][-2] if len(rows[label]) == 2 else None)
            else:
                clause = (r"From the preceding month, the PCE price index(?: for \w+)? " if key == "pce_mom"
                          else r"Excluding food and energy, the PCE price index (?:also )?")
                change = re.search(clause + r"(increased|decreased) (\d+(?:\.\d+)?) percent", text)
                if change is None:
                    raise ValueError(f"PCE 公告缺少精确月率：{key}")
                put(key, ("-" if change[1] == "decreased" else "") + change[2])
        annual = next((compact(p.xpath("string(.)").get()) for p in content.css("p")
                       if "From the same month one year ago" in p.xpath("string(.)").get()), "")
        changes = re.findall(r"(increased|decreased) (\d+(?:\.\d+)?) percent", annual)
        if len(changes) != 2 or "Excluding food and energy" not in annual:
            raise ValueError("PCE 公告缺少整体与核心年率")
        for key, (movement, amount) in zip(("pce_yoy", "core_pce_yoy"), changes, strict=True):
            put(key, ("-" if movement == "decreased" else "") + amount)
    elif family == "gdp":
        match = re.search(r"Real gross domestic product \(GDP\) (increased|decreased) at an annual rate of (\d+(?:\.\d+)?) percent", text, re.I)
        if match is None:
            raise ValueError("GDP 公告缺少实际年化季率")
        put("gdp", ("-" if match[1].lower() == "decreased" else "") + match[2])
    elif family == "fomc":
        match = re.search(r"target range for the federal funds rate.{0,100}? to (\d+(?:[.-]\d+(?:/\d+)?)?(?:/\d+)?) percent", text)
        if match is None:
            raise ValueError("FOMC 声明缺少利率目标上限")
        raw = match[1]
        if "/" in raw:
            whole, fraction = raw.split("-", 1) if "-" in raw else ("0", raw)
            numerator, denominator = fraction.split("/")
            raw = str(Decimal(whole) + Decimal(numerator) / Decimal(denominator))
        put("fed_rate", raw)
    else:
        raise ValueError(f"不支持的指标：{family}")
    return values
