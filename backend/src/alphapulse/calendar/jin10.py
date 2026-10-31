"""Normalize authenticated Jin10 calendar responses without interpreting gold/USD signals."""
import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo("Asia/Shanghai")
START = date(2025, 1, 1)
KINDS = ("data", "event", "holiday")
COUNTRY_CODES = {
    "美国": "US", "欧元区": "EA", "英国": "GB", "德国": "DE", "法国": "FR",
    "中国": "CN", "中国香港": "HK", "中国台湾": "TW", "日本": "JP", "韩国": "KR",
    "加拿大": "CA", "澳大利亚": "AU", "新西兰": "NZ", "瑞士": "CH", "瑞典": "SE",
    "挪威": "NO", "意大利": "IT", "西班牙": "ES", "印度": "IN", "俄罗斯": "RU",
    "巴西": "BR", "南非": "ZA", "新加坡": "SG", "乌克兰": "UA", "墨西哥": "MX",
    "土耳其": "TR", "波兰": "PL", "印度尼西亚": "ID", "马来西亚": "MY", "泰国": "TH",
}


@dataclass(frozen=True)
class Entry:
    identity: str
    at: datetime
    title: str
    values: dict


def day_url(day: date) -> str:
    return f"https://rili.jin10.com/day/{day.isoformat()}/"


def records(body: dict, kind: str, day: date) -> list[dict]:
    if kind not in KINDS or not isinstance(body, dict) or body.get("date") != day.isoformat():
        raise ValueError("金十响应日期或类别与请求不符")
    rows = body.get("list")
    if kind == "holiday":
        # The public SDK's legacy daily holiday method returns zlib/base64 text.
        if not isinstance(rows, str) or len(rows) > 2_000_000:
            raise ValueError("金十假期响应格式已变化")
        try:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(base64.b64decode(rows, validate=True), 4_000_000)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("金十假期压缩内容不完整或过大")
            rows = json.loads(decoded)
        except (ValueError, zlib.error, binascii.Error) as exc:
            raise ValueError("金十假期内容无法解析") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("金十日历列表格式已变化")
    return rows


def text(row: dict, key: str, *, nullable: bool = False) -> str | None:
    value = row.get(key)
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"金十字段 {key} 缺失或类型错误")
    return value.strip()


def identifier(row: dict, key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"金十字段 {key} 不是有效标识")
    return value


def normalize(body: dict, kind: str, day: date) -> list[Entry]:
    if day < START:
        raise ValueError("金十历史范围不得早于2025-01-01北京时间")
    result, identities = [], set()
    for raw in records(body, kind, day):
        identity = f"{kind}:{identifier(raw, 'data_id' if kind == 'data' else 'id')}"
        if identity in identities:
            raise ValueError("金十同一响应存在重复事件标识")
        identities.add(identity)
        country = text(raw, "country", nullable=True)
        precision = "day" if kind == "holiday" else "minute"
        if kind == "holiday":
            if raw.get("date") != day.isoformat():
                raise ValueError("金十假期记录日期与请求不符")
            at = datetime.combine(day, time.min, LOCAL)
            title = text(raw, "name")
            description = " · ".join(v for v in [text(raw, "exchange_name"), text(raw, "rest_note", nullable=True),
                                                  text(raw, "remark", nullable=True)] if v)
        else:
            timestamp = text(raw, "pub_time" if kind == "data" else "event_time")
            at = datetime.strptime(timestamp, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL)
            if at.date() != day:
                raise ValueError("金十记录公布日期与请求不符")
            unix = raw.get("pub_time_unix" if kind == "data" else "event_time_unix")
            if type(unix) is not int or not int(at.timestamp()) <= unix < int(at.timestamp()) + 60:
                raise ValueError("金十公布时间与时间戳不符")
            at = datetime.fromtimestamp(unix, LOCAL)
            if raw.get("determine") not in (0, 1):
                raise ValueError("金十时间确定性字段已变化")
            if raw["determine"] == 1 or raw.get("time_status"):
                precision = "tentative"
            title = text(raw, "indicator_name" if kind == "data" else "event_content")
            description = text(raw, "note", nullable=True) if kind == "event" else None
        if kind != "data" and raw.get("category") != "cj":
            raise ValueError("金十记录不属于宏观日历")
        importance = raw.get("star") if kind != "holiday" else 0
        if type(importance) is not int or not 0 <= importance <= 5:
            raise ValueError("金十重要性字段已变化")
        key = f"jin10_{identifier(raw, 'indicator_id')}" if kind == "data" else f"jin10_{kind}"
        meta = {"kind": kind, "date": day.isoformat(), "precision": precision, "country_name": country or "未标注",
                "country_code": COUNTRY_CODES.get(country, country or "UNKNOWN"),
                "name": title, "description": description, "importance": importance,
                "period_label": text(raw, "time_period", nullable=True) if kind == "data" else None,
                "indicator": key, "unit": ("" if raw.get("unit") == "" else text(raw, "unit", nullable=True)) if kind == "data" else None,
                "exchange": text(raw, "exchange_name") if kind == "holiday" else None}
        values = {"_jin10": meta}
        if kind == "data":
            # Preserve nonnumeric values (including ranges); never coerce a missing value to zero.
            values[key] = {"actual": text(raw, "actual", nullable=True), "previous": text(raw, "previous", nullable=True),
                           "forecast": text(raw, "consensus", nullable=True),
                           "previous_revised": text(raw, "revised", nullable=True)}
        result.append(Entry(identity, at.astimezone(UTC), title, values))
    return result


def period(label: str | None, at: datetime) -> str | None:
    """Resolve only verified monthly/weekly labels close to their publication date."""
    local = at.astimezone(LOCAL).date()
    month = re.fullmatch(r"(\d{1,2})月", label or "")
    week = re.fullmatch(r"至(\d{1,2})月(\d{1,2})日", label or "")
    if month:
        number = int(month[1])
        if not 1 <= number <= 12:
            return None
        year = local.year - (number > local.month)
        return f"{year}-{number:02}"
    if week:
        try:
            day = date(local.year, int(week[1]), int(week[2]))
            if day > local:
                day = day.replace(year=day.year - 1)
            return day.isoformat() if local - day < timedelta(days=45) else None
        except ValueError:
            return None
    quarter = re.fullmatch(r"第([一二三四])季度", label or "")
    if quarter:
        number = "一二三四".index(quarter[1]) + 1
        year = local.year - (number > (local.month - 1) // 3 + 1)
        return f"{year}-Q{number}"
    return None
