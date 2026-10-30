"""EIA commercial inventory changes from the official, unrounded Table 1 CSV."""
import csv
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
import re
from urllib.parse import urljoin, urlparse

from parsel import Selector

from alphapulse.calendar.archives import Announcement
from alphapulse.calendar.parsers import EASTERN, Scheduled, compact

SCHEDULE = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
ARCHIVE = "https://www.eia.gov/petroleum/supply/weekly/archive/"
LATEST = "https://ir.eia.gov/wpsr/table1.csv"
ROWS = {"Commercial (Excluding SPR)": "eia_crude", "Total Motor Gasoline": "eia_gasoline", "Distillate Fuel Oil": "eia_distillate"}


def exceptions(body: str) -> dict[date, datetime]:
    sel = Selector(text=body)
    text = compact(sel.xpath("string(//body)").get() or "")
    if "10:30 a.m. eastern time on Wednesday" not in text or "All times are eastern" not in text:
        raise ValueError("EIA 缺少已验证的每周公布规则")
    tables = sel.css("table.schedule")
    if len(tables) != 1 or "Data for the week ending" not in tables[0].xpath("string(.)").get():
        raise ValueError("EIA 缺少唯一的节假日例外表")
    result = {}
    for row in tables[0].css("tbody tr"):
        cells = [compact(n.xpath("string(.)").get()) for n in row.css("th,td")]
        if len(cells) != 5:
            raise ValueError("EIA 例外日程列格式已变化")
        period = datetime.strptime(cells[0], "%B %d, %Y").date()
        day = datetime.strptime(cells[1], "%B %d, %Y").date()
        clock = datetime.strptime(cells[3].replace("a.m.", "AM").replace("p.m.", "PM"), "%I:%M %p").time()
        if cells[2] != day.strftime("%A") or period in result:
            raise ValueError("EIA 例外日程星期或统计周不符")
        result[period] = datetime.combine(day, clock, EASTERN).astimezone(UTC)
    return result


def release_at(period: date, changes: dict[date, datetime]) -> datetime:
    if period.weekday() != 4:
        raise ValueError("EIA 统计期不是周五")
    return changes.get(period, datetime.combine(period + timedelta(days=5), time(10, 30), EASTERN).astimezone(UTC))


def schedule(body: str, now: datetime) -> list[Scheduled]:
    changes = exceptions(body)
    start = now.astimezone(EASTERN).date() - timedelta(days=60)
    result = []
    for i in range(160):
        period = start + timedelta(days=i)
        if period.weekday() == 4:
            result.append(Scheduled(f"eia-stocks-{period}", "eia_stocks", "Weekly Petroleum Status Report",
                                    release_at(period, changes), period=period.isoformat()))
    return result


def archive_links(body: str, start: date, end: date) -> list[str]:
    result = []
    for link in Selector(text=body).css("a[href]::attr(href)").getall():
        url = urljoin(ARCHIVE, link)
        parsed = urlparse(url)
        match = re.fullmatch(r"/petroleum/supply/weekly/archive/(20\d\d)/(\d{4}_\d{2}_\d{2})/wpsr_\2\.php", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "www.eia.gov" or match is None:
            continue
        day = datetime.strptime(match[2], "%Y_%m_%d").date()
        if day.year != int(match[1]):
            raise ValueError("EIA 归档年份与日期不符")
        if start <= day <= end:
            result.append(url)
    if not result:
        raise ValueError("EIA 归档没有所选范围的已公布报告")
    return list(dict.fromkeys(result))


def csv_link(body: str, base: str) -> str:
    links = [urljoin(base, link) for link in Selector(text=body).css("a[href]::attr(href)").getall()
             if link == "download/table1.csv"]
    if len(set(links)) != 1:
        raise ValueError("EIA 归档未唯一列示 Table 1 CSV")
    return links[0]


def report(body: str, schedule_body: str) -> Announcement:
    rows = csv.reader(StringIO(body))
    header = next(rows, [])
    if len(header) != 8 or header[0] != "STUB_1" or header[3] != "Difference":
        raise ValueError("EIA Table 1 CSV 表头已变化")
    period, previous = (datetime.strptime(value, "%m/%d/%y").date() for value in header[1:3])
    if period - previous != timedelta(days=7):
        raise ValueError("EIA Table 1 当前周与上周不连续")
    values = {}
    for row in rows:
        if row and row[0] == "STUB_1":
            break  # Following tables use daily supply rates, not inventory levels.
        if not row or row[0] not in ROWS:
            continue
        if len(row) != 8 or ROWS[row[0]] in values:
            raise ValueError("EIA 库存数据行重复或缺列")
        number = row[3].replace(",", "")
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", number):
            raise ValueError("EIA 库存变动不是有限数值")
        change = Decimal(number)
        # Table 1 inventories are million barrels; the calendar displays ten thousand barrels.
        # Previous inventory levels are not the previous week's inventory CHANGE.
        values[ROWS[row[0]]] = {"actual": format(change * 100, "f"), "previous": None}
    if values.keys() != set(ROWS.values()):
        raise ValueError("EIA 缺少商业原油、汽油或精炼油库存变动")
    return Announcement(release_at(period, exceptions(schedule_body)), period.isoformat(), None, values)
