"""Join only verified series definitions; official values win independently for each field."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation

from alphapulse.calendar.catalog import FAMILIES, INDICATORS
from alphapulse.calendar.jin10 import LOCAL, period
from alphapulse.calendar.service import impact
from alphapulse.models import EconomicRelease

# (Jin10 indicator, exact name) -> (official family, indicator, release stage).
# CPI annual rates are NSA, monthly rates SA; other adjustments must not be substituted.
US = {
    (234, "未季调CPI年率"): ("cpi", "cpi_yoy", None),
    (230, "季调后CPI月率"): ("cpi", "cpi_mom", None),
    (232, "未季调核心CPI年率"): ("cpi", "core_cpi_yoy", None),
    (233, "季调后核心CPI月率"): ("cpi", "core_cpi_mom", None),
    (178, "PPI年率"): ("ppi", "ppi_yoy", None),
    (177, "PPI月率"): ("ppi", "ppi_mom", None),
    (75, "季调后非农就业人口"): ("jobs", "payrolls", None),
    (76, "失业率"): ("jobs", "unemployment", None),
    (212, "PCE物价指数年率"): ("pce", "pce_yoy", None),
    (213, "PCE物价指数月率"): ("pce", "pce_mom", None),
    (211, "核心PCE物价指数年率"): ("pce", "core_pce_yoy", None),
    (214, "核心PCE物价指数月率"): ("pce", "core_pce_mom", None),
    (144, "实际GDP年化季率修正值"): ("gdp", "gdp", "second"),
    (872, "实际GDP年化季率终值"): ("gdp", "gdp", "third"),
    (198, "密歇根大学消费者信心指数初值"): ("umich", "umich_sentiment", "preliminary"),
    (208, "密歇根大学消费者信心指数终值"): ("umich", "umich_sentiment", "final"),
    (216, "实际GDP年化季率初值"): ("gdp", "gdp", "advance"),
    (77, "美联储利率决定(上限)"): ("fomc", "fed_rate", None),
    (78, "ADP就业人数"): ("adp_jobs", "adp_jobs", None),
    (79, "当周初请失业金人数"): ("initial_claims", "initial_claims", None),
    (96, "当周续请失业金人数"): ("continued_claims", "continued_claims", None),
    (111, "当周EIA原油库存"): ("eia_stocks", "eia_crude", None),
    (114, "当周EIA汽油库存"): ("eia_stocks", "eia_gasoline", None),
    (113, "当周EIA精炼油库存"): ("eia_stocks", "eia_distillate", None),
}
EA = {
    (763, "GDP季率初值"): ("ea_gdp", "gdp_qoq", "preliminary"),
    (762, "GDP年率初值"): ("ea_gdp", "gdp_yoy", "preliminary"),
    (930, "CPI年率初值"): ("ea_hicp", "hicp_yoy", "flash"),
    (413, "CPI月率初值"): ("ea_hicp", "hicp_mom", "flash"),
    (759, "核心CPI年率初值"): ("ea_hicp", "core_hicp_yoy", "flash"),
    (764, "CPI年率终值"): ("ea_hicp", "hicp_yoy", "final"),
    (8125, "CPI月率终值"): ("ea_hicp", "hicp_mom", "final"),
    (622, "核心CPI年率终值"): ("ea_hicp", "core_hicp_yoy", "final"),
    (396, "失业率"): ("ea_jobs", "ilo_unemployment", None),
    (395, "欧洲央行存款机制利率"): ("ecb_rate", "ecb_deposit", None),
    (753, "欧洲央行主要再融资利率"): ("ecb_rate", "ecb_main", None),
    (419, "欧洲央行边际贷款利率"): ("ecb_rate", "ecb_marginal", None),
}

FR = {
    (831, "GDP年率初值"): ("fr_gdp", "gdp_yoy", "initial"),
    (829, "GDP年率终值"): ("fr_gdp", "gdp_yoy", "final"),
    (830, "GDP季率终值"): ("fr_gdp", "gdp_qoq", "final"),
    (8111, "CPI年率初值"): ("fr_cpi", "eu_cpi_yoy", "preliminary"),
    (8112, "CPI月率初值"): ("fr_cpi", "eu_cpi_mom", "preliminary"),
    (8113, "调和CPI年率初值"): ("fr_cpi", "hicp_yoy", "preliminary"),
    (8114, "调和CPI月率初值"): ("fr_cpi", "hicp_mom", "preliminary"),
    (567, "CPI年率终值"): ("fr_cpi", "eu_cpi_yoy", "final"),
    (568, "CPI月率终值"): ("fr_cpi", "eu_cpi_mom", "final"),
    (569, "调和CPI年率终值"): ("fr_cpi", "hicp_yoy", "final"),
    (570, "调和CPI月率终值"): ("fr_cpi", "hicp_mom", "final"),
    (832, "GDP季率初值"): ("fr_gdp", "gdp_qoq", "initial"),
}
DE = {
    (270, "季调后GDP季率终值"): ("de_gdp", "gdp_qoq", "final"),
    (842, "工作日调整后GDP年率终值"): ("de_gdp", "gdp_yoy", "final"),
    (846, "CPI年率初值"): ("de_cpi", "eu_cpi_yoy", "preliminary"),
    (847, "CPI月率初值"): ("de_cpi", "eu_cpi_mom", "preliminary"),
    (839, "调和CPI年率初值"): ("de_cpi", "hicp_yoy", "preliminary"),
    (840, "调和CPI月率初值"): ("de_cpi", "hicp_mom", "preliminary"),
    (844, "CPI年率终值"): ("de_cpi", "eu_cpi_yoy", "final"),
    (845, "CPI月率终值"): ("de_cpi", "eu_cpi_mom", "final"),
    (837, "调和CPI年率终值"): ("de_cpi", "hicp_yoy", "final"),
    (838, "调和CPI月率终值"): ("de_cpi", "hicp_mom", "final"),
    (245, "季调后GDP季率初值"): ("de_gdp", "gdp_qoq", "initial"),
    (843, "工作日调整后GDP年率初值"): ("de_gdp", "gdp_yoy", "initial"),
}
GB = {
    (649, "三个月ILO失业率"): ("uk_jobs", "uk_unemployment", None),
    (390, "GDP年率初值"): ("uk_gdp", "gdp_yoy", "initial"),
    (391, "GDP季率初值"): ("uk_gdp", "gdp_qoq", "initial"),
    (385, "GDP年率终值"): ("uk_gdp", "gdp_yoy", "final"),
    (658, "GDP季率终值"): ("uk_gdp", "gdp_qoq", "final"),
    (344, "CPI年率"): ("uk_cpi", "eu_cpi_yoy", None),
    (348, "CPI月率"): ("uk_cpi", "eu_cpi_mom", None),
    (349, "核心CPI年率"): ("uk_cpi", "eu_core_cpi_yoy", None),
    (638, "央行利率决定"): ("boe_rate", "bank_rate", None),
}
MAPPINGS = {"US": US, "EA": EA, "FR": FR, "DE": DE, "GB": GB}


def definition(meta: dict) -> tuple[str, str, str | None] | None:
    if meta["kind"] != "data" or meta["precision"] != "minute":
        return None
    mapping = MAPPINGS.get(meta["country_code"], {})
    match = mapping.get((int(meta["indicator"].removeprefix("jin10_")), meta["name"]))
    # Jin10 omits units for the verified Michigan index, whose official series is an index in points.
    unit = "点" if meta["unit"] is None and meta["country_code"] == "US" and meta["indicator"] in ("jin10_198", "jin10_208") else meta["unit"]
    return match if match and unit == INDICATORS[match[1]].unit else None


def provenance(source: str, at: str | datetime | None, url: str) -> dict:
    return {"source": source, "observed_at": at, "url": url}


def jin10_item(row: EconomicRelease, now: datetime) -> dict:
    meta = row.values["_jin10"]
    key = meta["indicator"]
    value = row.values.get(key, {})
    actual, previous, forecast = value.get("actual"), value.get("previous"), value.get("forecast")
    revised = value.get("previous_revised")
    match = definition(meta)
    eligible = value.get("forecast_before_release", False)
    if row.schedule_status == "withdrawn":
        status = "withdrawn"
        direction, explanation = "uncertain", "金十已移除此日程；已保存值仅供追溯，不作为当前有效日程或方向判断。"
    elif meta["kind"] != "data":
        status = "recorded"
        direction, explanation = "uncertain", "日程记录不代表事件已经发生；需结合实际消息判断 BTC/ETH 影响。"
    else:
        status = "released" if actual is not None else "historical" if (now - row.scheduled_at).days > 45 else "scheduled" if now < row.scheduled_at else "delayed"
        direction, explanation = direction_for(match[1] if match else None, actual, revised if revised is not None else previous, forecast if eligible else None)
    observed = value.get("observed_at", {})
    fields = {field: provenance("jin10", observed.get(field), row.schedule_url)
              for field in ("actual", "previous", "forecast", "previous_revised") if value.get(field) is not None}
    fields["scheduled_at"] = provenance("jin10", row.schedule_checked_at, row.schedule_url)
    if revised is not None:
        if "previous" in fields:
            fields["previous_original"] = fields["previous"]
        fields["previous"] = fields["previous_revised"]
    return {"release_id": row.id, "indicator": key, "name": meta["name"], "unit": meta["unit"],
            "country_code": meta["country_code"], "country_name": meta["country_name"],
            "event_name": f"{meta['country_name']}{meta['period_label'] or ''}{meta['name']}",
            "period": None, "period_label": meta["period_label"], "stage": None, "stage_name": None,
            "title": meta["description"] or meta["name"], "source": "jin10", "scheduled_at": row.scheduled_at,
            "calendar_date": meta["date"], "time_precision": meta["precision"], "kind": meta["kind"],
            "importance": meta["importance"], "importance_source": "jin10", "status": status,
            "actual": actual, "previous": revised if revised is not None else previous,
            "previous_original": previous, "previous_revised": revised, "forecast": forecast,
            "previous_basis": "金十列示的修订前值（原前值单独保留）" if revised is not None else "金十本次记录列示前值" if previous is not None else None,
            "forecast_before_release": eligible, "forecast_observed_at": observed.get("forecast"),
            "impact": direction, "explanation": explanation, "impact_basis": "forecast" if forecast is not None and eligible else "previous" if previous is not None or revised is not None else None,
            "release_url": None, "schedule_url": row.schedule_url, "first_received_at": row.first_received_at,
            "last_checked_at": row.last_checked_at, "error": row.error, "revision_no": row.revision_no,
            "field_sources": fields, "related_release_id": None, "related_revision_no": None}


def direction_for(key: str | None, actual: str | None, previous: str | None, forecast: str | None) -> tuple[str, str]:
    if key is None:
        return "uncertain", "尚无经核实的 BTC/ETH 传导判断；金十的黄金/美元影响标签未用于加密资产。"
    try:
        if any(not Decimal(v).is_finite() for v in (actual, previous, forecast) if v is not None):
            raise InvalidOperation()
        return impact(key, actual, previous, forecast)
    except InvalidOperation:
        return "uncertain", "公布值或比较基准不是单一数值，暂不能判断 BTC/ETH 方向。"


def merge_items(official: list[tuple[EconomicRelease, dict]], third_party: list[EconomicRelease], now: datetime) -> list[dict]:
    output = []
    used = set()
    for row, original in official:
        item = deepcopy(original)
        item.update(kind="data", time_precision="minute", calendar_date=row.scheduled_at.astimezone(LOCAL).date().isoformat(),
                    importance_source="project", field_sources={field: provenance(row.source, row.first_received_at, row.release_url or row.schedule_url)
                    for field in ("actual", "previous", "forecast") if item[field] is not None},
                    related_release_id=None, related_revision_no=None, period_label=None, previous_original=None, previous_revised=None,
                    forecast_before_release=False, forecast_observed_at=None)
        item["field_sources"]["scheduled_at"] = provenance(row.source, row.schedule_checked_at, row.schedule_url)
        matches = []
        for extra in third_party:
            if extra.schedule_status == "withdrawn":
                continue
            meta = extra.values["_jin10"]
            match = definition(meta)
            if not match or match != (row.family, item["indicator"], row.stage):
                continue
            if FAMILIES[row.family].source != row.source or extra.scheduled_at != row.scheduled_at:
                continue
            # Rate decisions have no statistical reference period. All data series must agree on it.
            if row.family not in ("fomc", "ecb_rate", "boe_rate") and (row.period is None or period(meta["period_label"], extra.scheduled_at) != row.period):
                continue
            matches.append(extra)
        if len(matches) == 1:
            extra = matches[0]
            fallback = jin10_item(extra, now)
            used.add(extra.id)
            item.update(related_release_id=extra.id, related_revision_no=extra.revision_no,
                        previous_original=fallback["previous_original"], previous_revised=fallback["previous_revised"],
                        forecast_before_release=fallback["forecast_before_release"], forecast_observed_at=fallback["forecast_observed_at"])
            for field in ("actual", "previous", "forecast"):
                if item[field] is None and fallback[field] is not None:
                    item[field] = fallback[field]
                    provider_field = "previous_revised" if field == "previous" and fallback["previous_revised"] is not None else field
                    item["field_sources"][field] = fallback["field_sources"][provider_field]
                    if field == "previous":
                        item["previous_basis"] = fallback["previous_basis"]
                    if field == "actual":
                        item["first_received_at"] = fallback["first_received_at"]
            if item["actual"] is not None and item["status"] != "withdrawn":
                item["status"] = "released"
            forecast = item["forecast"] if item.get("forecast_before_release") else None
            item["impact"], item["explanation"] = direction_for(item["indicator"], item["actual"], item["previous"], forecast)
            item["impact_basis"] = "forecast" if forecast is not None else "previous" if item["previous"] is not None else None
        output.append(item)
    output.extend(jin10_item(row, now) for row in third_party if row.id not in used)
    return sorted(output, key=lambda r: (r["calendar_date"], r["scheduled_at"], r["release_id"], r["indicator"]))
