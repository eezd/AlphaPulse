"""DOL weekly claims: use published schedules and the seasonally adjusted release."""
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlparse

from parsel import Selector

from alphapulse.calendar.archives import Announcement, publication_time
from alphapulse.calendar.parsers import EASTERN, MONTHS, Scheduled, compact

SCHEDULE = "https://oui.doleta.gov/unemploy/archive.asp"
LATEST = "https://www.dol.gov/ui/data.pdf"
FAMILIES = ("initial_claims", "continued_claims")
MONTH_PATTERN = "(?:" + "|".join(MONTHS) + ")"


def schedule(body: str, now: datetime) -> list[Scheduled]:
    sel = Selector(text=body)
    text = compact(sel.xpath("string(//body)").get() or "")
    if not re.search(r"Publication Schedule:.*Thursday morning at 8:30am EST", text):
        raise ValueError("DOL 缺少已验证的每周公布规则")
    exceptions = {}
    tables = [table for table in sel.css("table") if {compact(n.xpath("string(.)").get()) for n in table.css("th")} == {"Release Date", "Release Time"}]
    if len(tables) != 1:
        raise ValueError("DOL 缺少唯一的节假日例外表")
    for row in tables[0].css("tbody tr"):
        cells = [compact(n.xpath("string(.)").get()) for n in row.css("td")]
        if len(cells) != 2 or not re.fullmatch(r"\w+, " + MONTH_PATTERN + r" \d{1,2}, 20\d\d", cells[0]):
            raise ValueError("DOL 例外公布日期格式已变化")
        day = datetime.strptime(cells[0], "%A, %B %d, %Y").date()
        if cells[1] != "8:30 AM EST":
            raise ValueError("DOL 例外公布时刻已变化")
        thursday = day + timedelta(days=3 - day.weekday())
        if thursday in exceptions:
            raise ValueError("DOL 同周公布例外不唯一")
        exceptions[thursday] = day
    start = now.astimezone(EASTERN).date() - timedelta(days=45)
    end = now.astimezone(EASTERN).date() + timedelta(days=90)
    days = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    result = []
    for thursday in sorted(day for day in days if day.weekday() == 3):
        day = exceptions.get(thursday, thursday)
        at = datetime.combine(day, time(8, 30), EASTERN).astimezone(UTC)
        for family in FAMILIES:
            result.append(Scheduled(f"dol-{family}-{day}", family, "Unemployment Insurance Weekly Claims", at))
    return result


def archive_links(body: str, start: date, end: date) -> list[str]:
    links = []
    for link in Selector(text=body).css("a[href]::attr(href)").getall():
        url = urljoin(SCHEDULE, link)
        parsed = urlparse(url)
        match = re.fullmatch(r"/press/(20\d\d)/(\d{6})\.pdf", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "oui.doleta.gov" or match is None:
            continue
        day = datetime.strptime(match[2], "%m%d%y").date()
        if day.year != int(match[1]):
            raise ValueError("DOL 归档路径年份与日期不符")
        if start <= day <= end:
            links.append(url)
    if not links:
        raise ValueError("DOL 归档未列出所选范围的报告，历史覆盖尚未确认")
    return list(dict.fromkeys(links))


def report(text: str) -> dict[str, Announcement]:
    text = compact(text)
    if "UNEMPLOYMENT INSURANCE WEEKLY CLAIMS" not in text:
        raise ValueError("DOL 响应不是失业金周报")
    at = publication_time(text)
    match = re.search(r"SEASONALLY ADJUSTED DATA (.*?) UNADJUSTED DATA", text)
    if match is None:
        raise ValueError("DOL 缺少独立的季调数据正文")
    adjusted = match[1]
    initial = re.search(r"In the week ending (" + MONTH_PATTERN + r" \d{1,2}), the advance figure for seasonally adjusted initial claims was ([\d,]+)", adjusted)
    continued = re.search(r"advance number for seasonally adjusted insured unemployment during the week ending (" + MONTH_PATTERN + r" \d{1,2}) was ([\d,]+)", adjusted)
    if initial is None or continued is None:
        raise ValueError("DOL 缺少初请或续请季调读数")
    result = {}
    for family, reading in zip(FAMILIES, (initial, continued), strict=True):
        # Resolve year rollover from the official publication date, never from collection time.
        period = datetime.strptime(f"{reading[1]}, {at.astimezone(EASTERN).year}", "%B %d, %Y").date()
        published = at.astimezone(EASTERN).date()
        if period > published:
            period = period.replace(year=period.year - 1)
        if not 0 <= (published - period).days <= 21:
            raise ValueError("DOL 统计周与公布日期不符")
        following = adjusted[reading.end():].split("The 4-week moving average", 1)[0]
        previous = re.search(r"previous week's level was (?:revised (?:up|down) by [\d,]+ from [\d,]+ to|unrevised at) ([\d,]+)", following)
        if previous is None:
            previous = re.search(r"previous week's unrevised level of ([\d,]+)", following)
        if previous is None:
            raise ValueError("DOL 缺少本报告列示的可比前值")
        value = {"actual": format(Decimal(reading[2].replace(",", "")) / 10000, "f"),
                 "previous": format(Decimal(previous[1].replace(",", "")) / 10000, "f"),
                 "previous_basis": "DOL 本次季调报告列示的上周值（可能已修订）"}
        result[family] = Announcement(at, period.isoformat(), None, {family: value})
    return result
