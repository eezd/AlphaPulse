"""官方当期公告解析；归档内容不被标为公布时就已采集的原始版本。"""
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from parsel import Selector

from alphapulse.calendar.parsers import EASTERN, MONTHS, cells, compact, number, parse_values, release_metadata
from alphapulse.calendar.umich import pdf_text

MONTH_PATTERN = "(?:" + "|".join(MONTHS) + ")"
MOVEMENT = r"(increased|rose|advanced|edged up|moved up|declined|decreased|fell|edged down|moved down)\s+([\d.]+)\s*percent"


@dataclass(frozen=True)
class Announcement:
    at: datetime
    period: str | None
    stage: str | None
    values: dict


def signed(match: re.Match) -> str:
    sign = "-" if match[1].lower() in {"declined", "decreased", "fell", "edged down", "moved down"} else ""
    return number(sign + match[2])


def publication_time(text: str) -> datetime:
    date = re.search(MONTH_PATTERN + r"\s+\d{1,2},\s*20\d\d", text)
    clock = re.search(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", text, re.I)
    if date is None or clock is None:
        raise ValueError("官方公告缺少正式发布日期或公布时刻")
    day = datetime.strptime(re.sub(r",\s*", ", ", date[0]), "%B %d, %Y")
    hour = int(clock[1]) % 12 + (12 if clock[3].lower() == "p" else 0)
    return day.replace(hour=hour, minute=int(clock[2]), tzinfo=EASTERN).astimezone(UTC)


def parse_bls_text(family: str, text: str) -> dict:
    """PDF 的表格字体可能无法提取；只读取正文明确叙述的当期读数。"""
    text = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", compact(text)).replace("−", "-")
    if family == "jobs":
        # 与实时 HTML 使用同一就业正文契约，不复制非农单位与负值规则。
        from html import escape
        return parse_values(family, '<div id="bodytext">' + escape(text) + '</div>')

    def movement(pattern: str) -> str:
        match = re.search(pattern, text, re.I)
        if match is None:
            raise ValueError(f"BLS {family} 公告缺少明确的当期变化：{pattern}")
        return signed(match)

    if family == "cpi":
        monthly = re.search(r"Consumer Price Index for All Urban Consumers.{0,80}?" + MOVEMENT, text, re.I)
        if monthly:
            mom = signed(monthly)
        elif re.search(r"Consumer Price Index for All Urban Consumers.{0,80}?(?:was |remained )?unchanged", text, re.I):
            mom = "0"
        else:
            raise ValueError("CPI 公告缺少整体月率")
        yoy = movement(r"(?:all items index|index for all items)\s+" + MOVEMENT + r"\s+(?:over|for)\s+the\s+(?:last\s+)?12\s*months")
        core = re.search(r"(?:index for all items less food and energy|all items less food and energy index)\s+" + MOVEMENT, text, re.I)
        if core:
            core_mom = signed(core)
        elif re.search(r"(?:index for all items less food and energy|all items less food and energy index)\s+(?:was |remained )?unchanged", text, re.I):
            core_mom = "0"
        else:
            raise ValueError("CPI 公告缺少核心月率")
        core_yoy = movement(r"(?:all items less food and energy index|index for all items less food and energy)\s+" + MOVEMENT + r"\s+(?:over|for)\s+the\s+(?:last\s+)?12\s*months")
        return {key: {"actual": value, "previous": None} for key, value in
                [("cpi_mom", mom), ("cpi_yoy", yoy), ("core_cpi_mom", core_mom), ("core_cpi_yoy", core_yoy)]}
    if family == "ppi":
        match = re.search(r"Producer Price Index for final demand\s+" + MOVEMENT, text, re.I)
        if match:
            mom = signed(match)
        elif re.search(r"Producer Price Index for final demand\s+(?:was |remained |were )?unchanged", text, re.I):
            mom = "0"
        else:
            raise ValueError("PPI 公告缺少最终需求月率")
        yoy = movement(r"(?:index for final demand|final demand prices)\s+" + MOVEMENT +
                       r"\s+(?:in\s+20\d\d|(?:for|over)\s+the\s+(?:last\s+)?12[ -]month)")
        return {"ppi_mom": {"actual": mom, "previous": None}, "ppi_yoy": {"actual": yoy, "previous": None}}
    raise ValueError(f"不支持的 BLS PDF 指标：{family}")


def parse_announcement(family: str, body: bytes, *, pdf: bool = False) -> Announcement:
    if pdf:
        text = compact(pdf_text(body, 1))
        values = parse_bls_text(family, text)
        title = text
    else:
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            if family not in {"cpi", "ppi", "jobs"}:
                raise
            # 已核实的旧 BLS HTML 混用 Windows-1252 标点；保留原字节，限定此来源解码。
            html = body.decode("cp1252")
        sel = Selector(text=html, type="html")
        text = compact(sel.xpath("string(//body)").get())
        title = compact(" ".join(sel.css("h1,#bodytext").xpath("string(.)").getall()))
        if family == "pce" and re.search(r"[A-Z][a-z]+ and [A-Z][a-z]+ 20\d\d", title):
            raise ValueError("PCE 多统计期合并公告需分别核对各月，未将最后一列冒充全部月份")
        values = parse_values(family, html)
    at = publication_time(text)
    if family in {"cpi", "ppi", "jobs"}:
        label = {"cpi": "CONSUMER PRICE INDEX", "ppi": "PRODUCER PRICE INDEXES", "jobs": "EMPLOYMENT SITUATION"}[family]
        heading = re.search(label + r"\s*(?:[—–]|-{1,2})\s*(" + MONTH_PATTERN + r"\s+20\d\d)", title, re.I)
        period, stage = release_metadata(family, heading[1]) if heading else (None, None)
    else:
        period, stage = release_metadata(family, title)
    if family != "fomc" and period is None:
        raise ValueError("官方公告缺少统计期")
    if family == "gdp" and stage is None:
        raise ValueError("GDP 公告缺少发布阶段")
    return Announcement(at, period, stage, values)


def parse_announcements(family: str, body: bytes, *, pdf: bool = False) -> list[Announcement]:
    if family != "pce" or pdf:
        return [parse_announcement(family, body, pdf=pdf)]
    sel = Selector(text=body.decode("utf-8"), type="html")
    title = compact(" ".join(sel.css("h1").xpath("string(.)").getall()))
    combined = re.search(r"(" + MONTH_PATTERN + r") and (" + MONTH_PATTERN + r") (20\d\d)", title)
    if combined is None:
        return [parse_announcement(family, body)]
    rows = [cells(n) for n in sel.css("table tr")]
    if ["", combined[1], combined[2]] not in rows:
        raise ValueError("PCE 合并公告表头未明确列示两个统计月")
    monthly = {r[0].replace("index,", "index"): r[1:] for r in rows if len(r) == 3}
    annual = next((compact(p.xpath("string(.)").get()) for p in sel.css(".field--name-body p")
                   if "From the same month one year ago" in p.xpath("string(.)").get()), "")
    changes = re.findall(r"(increased|decreased|increase|decrease) (?:of )?(\d+(?:\.\d+)?) percent", annual)
    if len(changes) != 4 or "Excluding food and energy" not in annual:
        raise ValueError("PCE 合并公告缺少两个统计月的整体和核心年率")
    # 数值按明确表头分别读取，不把另一统计月标为前次公告值。
    at = publication_time(compact(sel.xpath("string(//body)").get()))
    result = []
    for i, month in enumerate((combined[1], combined[2])):
        values = {}
        for key, label in (("pce_mom", "PCE price index"), ("core_pce_mom", "PCE price index excluding food and energy")):
            values[key] = {"actual": number(monthly[label][i]), "previous": None}
        for key, offset in (("pce_yoy", 0), ("core_pce_yoy", 2)):
            movement, value = changes[offset + i]
            values[key] = {"actual": number(("-" if movement.startswith("decreas") else "") + value), "previous": None}
        result.append(Announcement(at, f"{combined[3]}-{MONTHS[month]:02d}", None, values))
    return result
