"""密歇根公开日程、当前公告和月度终值；三者的时间与版本口径分开。"""
import csv
import re
from datetime import UTC, datetime
from io import BytesIO, StringIO
from urllib.parse import urljoin

from parsel import Selector
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from alphapulse.calendar.parsers import EASTERN, MONTHS, Scheduled, cells, compact, number


def pdf_text(body: bytes, pages: int | None = None) -> str:
    try:
        reader = PdfReader(BytesIO(body))
        text = "\n".join(page.extract_text() for page in reader.pages[:pages])
    except PdfReadError as exc:
        raise ValueError("官方 PDF 无法读取") from exc
    if not text.strip():
        raise ValueError("官方 PDF 缺少可提取文字")
    return text


def document_links(body: str, base: str, labels: set[str]) -> dict[str, str]:
    links = {compact(a.xpath("string(.)").get()): urljoin(base, a.attrib["href"])
             for a in Selector(text=body, type="html").css("a[href]")}
    return {label: links[label] for label in labels if label in links}


def report_links(body: str, year: int) -> dict[tuple[str, str], str]:
    """年度索引只取初值、终值报告，不把数据手册或专题报告当作当期公告。"""
    sel = Selector(text=body, type="html")
    if compact(sel.css("#report_heading .title").xpath("string(.)").get()) != f"{year} Reports":
        raise ValueError("密歇根历史报告索引年份不符")
    reports = {}
    for node in sel.css("a[href]"):
        match = re.fullmatch(r"([A-Z][a-z]+) (Preliminary|Final) Results", compact(node.xpath("string(.)").get()))
        if match is None:
            continue
        identity = (f"{year}-{MONTHS[match[1]]:02d}", "preliminary" if match[2] == "Preliminary" else "final")
        url = urljoin("https://data.sca.isr.umich.edu/", node.attrib["href"])
        if not re.fullmatch(r"https://data\.sca\.isr\.umich\.edu/fetchdoc\.php\?docid=\d+", url):
            raise ValueError("密歇根历史报告链接不是官方文档地址")
        if identity in reports:
            raise ValueError("密歇根历史报告索引存在重复期次")
        reports[identity] = url
    if not reports:
        raise ValueError("密歇根年度索引没有可识别的初值或终值报告")
    return reports


def parse_schedule(text: str) -> list[Scheduled]:
    events = []
    # 历史 PDF 明确列出调查月份、初值日期和终值日期，包括跨月公布。
    for match in re.finditer(r"\b([A-Z][a-z]{2})\s+(20\d\d)\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})", text):
        month = datetime.strptime(match[1], "%b").month
        period = f"{match[2]}-{month:02d}"
        for stage, value in zip(("preliminary", "final"), (match[3], match[4]), strict=True):
            at = datetime.strptime(value, "%m/%d/%Y").replace(hour=10, tzinfo=EASTERN).astimezone(UTC)
            events.append(Scheduled(f"umich-{period}-{stage}", "umich", "University of Michigan Consumer Sentiment", at,
                                    period=period, stage=stage))
    if events:
        if len({e.external_id for e in events}) != len(events):
            raise ValueError("密歇根日程存在重复期次")
        return events
    year = re.search(r"RELEASE DATES FOR (20\d\d)", text)
    if year:
        for match in re.finditer(r"([A-Z][a-z]+)\s+(\d{1,2})\s+([A-Z][a-z]+)\s+(Prelim|Final)\b", text):
            period = f"{year[1]}-{MONTHS[match[3]]:02d}"
            stage = "preliminary" if match[4] == "Prelim" else "final"
            at = datetime(int(year[1]), MONTHS[match[1]], int(match[2]), 10, tzinfo=EASTERN).astimezone(UTC)
            events.append(Scheduled(f"umich-{period}-{stage}", "umich", "University of Michigan Consumer Sentiment", at,
                                    period=period, stage=stage))
    if not events:
        raise ValueError("密歇根日程 PDF 缺少可识别的公布日期")
    if len({e.external_id for e in events}) != len(events):
        raise ValueError("密歇根日程存在重复期次")
    return events


def current_result(body: str) -> tuple[str, str, dict]:
    sel = Selector(text=body, type="html")
    heading = compact(sel.css("h1").xpath("string(.)").get())
    match = re.fullmatch(r"(Preliminary|Final) Results for ([A-Z][a-z]+) (20\d\d)", heading)
    if not match:
        raise ValueError("密歇根当前公告缺少统计月份或发布阶段")
    period = f"{match[3]}-{MONTHS[match[2]]:02d}"
    stage = "preliminary" if match[1] == "Preliminary" else "final"
    rows = [cells(r) for r in sel.css("#front_table tr")]
    if len(rows) < 3 or any(len(row) < 3 for row in rows) or rows[0][1] != match[2][:3] or rows[1][1] != match[3]:
        raise ValueError("密歇根表头与公告期次不符")
    row = next((r for r in rows if r[0] == "Index of Consumer Sentiment"), None)
    if row is None:
        raise ValueError("密歇根公告缺少消费者信心指数")
    # 官网列的是上月终值，不能标成本期初值；两者在详情中明确区别。
    previous_period = datetime.strptime(rows[0][2] + " " + rows[1][2], "%b %Y").strftime("%Y-%m")
    return period, stage, {"umich_sentiment": {"actual": number(row[1]), "previous": number(row[2]),
                                              "previous_basis": f"{previous_period} 官方上月终值"}}


def monthly_series(body: str) -> list[tuple[str, str]]:
    reader = csv.DictReader(StringIO(body.lstrip("\ufeff")))
    if reader.fieldnames != ["Month", "YYYY", "ICS_ALL"]:
        raise ValueError("密歇根月度 CSV 列名不符")
    result = []
    for row in reader:
        period = f"{int(row['YYYY']):04d}-{MONTHS[row['Month']]:02d}"
        result.append((period, number(row["ICS_ALL"])))
    if not result or len({p for p, _ in result}) != len(result):
        raise ValueError("密歇根月度 CSV 为空或统计期重复")
    return result
