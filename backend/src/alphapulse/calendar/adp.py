"""Read monthly ADP releases; weekly NER Pulse estimates are a separate series."""
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlparse

from parsel import Selector

from alphapulse.calendar.archives import Announcement
from alphapulse.calendar.parsers import EASTERN, MONTHS, Scheduled, compact

INDEX = "https://mediacenter.adp.com/press-releases?keywords=National+Employment+Report"
TITLE = "ADP National Employment Report"
MONTH = "(?:" + "|".join(MONTHS) + ")"
NUMBER = r"-?\d{1,3}(?:,\d{3})*"
NEXT_RELEASE = re.compile(r"The (" + MONTH + r") (20\d\d) ADP[^.]*?National Employment Report[^.]*?will be released (?:at 8:15 a\.m\. ET on (" + MONTH + r" \d{1,2}, 20\d\d)|on (" + MONTH + r" \d{1,2}, 20\d\d) at 8:15 a\.m\. ET)")


def monthly_title(title: str) -> bool:
    return bool(re.match(r"ADP National Employment Report: Private[- ]Sector Employment (?:Increased|Shed|Decreased|Unchanged)", title, re.I))


def release_links(body: str, base: str, start: date, end: date) -> tuple[list[str], str | None]:
    sel = Selector(text=body)
    rows = sel.css(".wd_item_list .wd_item")
    if not rows:
        raise ValueError("ADP 官方新闻索引缺少公告列表")
    links, dates = [], []
    for row in rows:
        day = datetime.strptime(compact(row.css(".wd_date").xpath("string(.)").get() or ""), "%b %d, %Y").date()
        dates.append(day)
        title = compact(row.css(".wd_title").xpath("string(.)").get() or "")
        if start <= day <= end and monthly_title(title):
            url = urljoin(base, row.css(".wd_title a::attr(href)").get() or "")
            if urlparse(url).scheme != "https" or urlparse(url).hostname != "mediacenter.adp.com" or "ADP-National-Employment-Report-" not in urlparse(url).path:
                raise ValueError("ADP 公告链接不是官方月报地址")
            links.append(url)
    if dates != sorted(dates, reverse=True):
        raise ValueError("ADP 官方新闻索引不再按公布日期倒序")
    following = sel.css(".wd_page_next a::attr(href)").get()
    # The index is newest first; continue through pages containing only weekly estimates.
    following = urljoin(base, following) if following and min(dates) >= start else None
    if following and (urlparse(following).hostname != "mediacenter.adp.com" or urlparse(following).path != "/press-releases" or urlparse(following).scheme != "https"):
        raise ValueError("ADP 分页链接不属于官方新闻索引")
    return list(dict.fromkeys(links)), following


def report(body: str) -> Announcement:
    sel = Selector(text=body)
    titles = sel.css(".wd_title").xpath("string(.)").getall()
    texts = sel.css(".wd_news_body").xpath("string(.)").getall()
    dates = sel.css(".wd_date").xpath("string(.)").getall()
    if len(titles) != 1 or len(texts) != 1 or len(dates) != 1 or not monthly_title(compact(titles[0])):
        raise ValueError("ADP 响应不是唯一的月度就业公告")
    text = compact(texts[0])
    period = re.search(r"(" + MONTH + r") (20\d\d) Report Highlights", text)
    reading = re.search(r"Change in U\.S\. Private Employment:\s*(" + NUMBER + r")(?![\d,])", text)
    if period is None or reading is None:
        raise ValueError("ADP 月报缺少统计期或私营就业净变动")
    month = MONTHS[period[1]]
    statistical = date(int(period[2]), month, 1)
    day = datetime.strptime(compact(dates[0]), "%b %d, %Y").date()
    # Monthly reports explicitly announce the next release at 8:15 a.m. ET.
    # Require that cadence in each document instead of guessing an unknown time.
    if NEXT_RELEASE.search(text) is None:
        raise ValueError("ADP 公告缺少明确的月报美东公布时刻")
    if not 0 <= (day - statistical).days <= 45:
        raise ValueError("ADP 统计月与公告日期不符")
    title_month = re.search(r"Jobs in (" + MONTH + r")", compact(titles[0]), re.I)
    if title_month is None or title_month[1].lower() != period[1].lower():
        raise ValueError("ADP 标题与正文统计月不符")
    headline = re.search(r"Employment (Increased by|Shed|Decreased by) (" + NUMBER + r") Jobs", compact(titles[0]), re.I)
    headline_value = Decimal(headline[2].replace(",", "")) * (1 if headline[1].lower() == "increased by" else -1) if headline else None
    if headline_value != Decimal(reading[1].replace(",", "")):
        raise ValueError("ADP 标题与正文的就业净变动不符")
    previous_month = (statistical - timedelta(days=1)).strftime("%B")
    previous = re.search(r"The " + previous_month + r" total (?:number of )?jobs added was revised from " + NUMBER + r" to (" + NUMBER + r")(?![\d,])", text)
    values = {"actual": format(Decimal(reading[1].replace(",", "")) / 10000, "f"),
              "previous": format(Decimal(previous[1].replace(",", "")) / 10000, "f") if previous else None}
    if previous:
        values["previous_basis"] = "ADP 本次月报列示的上月修订净变动"
    return Announcement(datetime.combine(day, time(8, 15), EASTERN).astimezone(UTC), statistical.strftime("%Y-%m"), None,
                        {"adp_jobs": values})


def next_release(body: str) -> Scheduled:
    text = compact(" ".join(Selector(text=body).css(".wd_news_body").xpath("string(.)").getall()))
    match = NEXT_RELEASE.search(text)
    if match is None:
        raise ValueError("ADP 公告缺少明确的下次月报日期与美东时刻")
    day = datetime.strptime(match[3] or match[4], "%B %d, %Y").date()
    period = date(int(match[2]), MONTHS[match[1]], 1)
    if not 0 <= (day - period).days <= 45:
        raise ValueError("ADP 下次公布日期与统计月不符")
    current = re.search(r"(" + MONTH + r") (20\d\d) Report Highlights", text)
    if current is None:
        raise ValueError("ADP 缺少本次月报统计期")
    expected_next = (date(int(current[2]), MONTHS[current[1]], 28) + timedelta(days=4)).replace(day=1)
    if period != expected_next:
        raise ValueError("ADP 下次月报不是相邻统计月")
    published = compact(Selector(text=body).css(".wd_date").xpath("string(.)").get() or "")
    if day <= datetime.strptime(published, "%b %d, %Y").date():
        raise ValueError("ADP 下次月报日期不晚于本次公告")
    return Scheduled(f"adp-{period:%Y-%m}", "adp_jobs", TITLE, datetime.combine(day, time(8, 15), EASTERN).astimezone(UTC), period=period.strftime("%Y-%m"))


def schedule(body: str, url: str) -> list[Scheduled]:
    current = report(body)
    return [Scheduled(f"adp-{current.period}", "adp_jobs", TITLE, current.at, period=current.period, release_url=url), next_release(body)]
