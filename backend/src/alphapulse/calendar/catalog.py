from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Indicator:
    name: str
    importance: int
    unit: str = "%"
    rate_channel: Literal["inverse", "ambiguous"] = "ambiguous"


@dataclass(frozen=True)
class Family:
    country_code: str
    source: str
    indicators: tuple[str, ...]


COUNTRIES = {"US": "美国", "EA": "欧元区", "GB": "英国", "DE": "德国", "FR": "法国"}


INDICATORS = {
    # 项目分级，不冒充第三方星级；rate_channel 仅描述利率传导下的参考方向。
    "cpi_mom": Indicator("CPI 月率", 4, rate_channel="inverse"),
    "cpi_yoy": Indicator("CPI 年率", 5, rate_channel="inverse"),
    "core_cpi_mom": Indicator("核心 CPI 月率", 5, rate_channel="inverse"),
    "core_cpi_yoy": Indicator("核心 CPI 年率", 5, rate_channel="inverse"),
    "ppi_mom": Indicator("PPI 最终需求月率", 3, rate_channel="inverse"),
    "ppi_yoy": Indicator("PPI 最终需求年率", 3, rate_channel="inverse"),
    "payrolls": Indicator("非农就业新增", 5, "万人"),
    "unemployment": Indicator("失业率", 4),
    "pce_mom": Indicator("PCE 物价月率", 3, rate_channel="inverse"),
    "core_pce_mom": Indicator("核心 PCE 物价月率", 4, rate_channel="inverse"),
    "pce_yoy": Indicator("PCE 物价年率", 4, rate_channel="inverse"),
    "core_pce_yoy": Indicator("核心 PCE 物价年率", 5, rate_channel="inverse"),
    "gdp": Indicator("实际 GDP 年化季率", 4),
    "fed_rate": Indicator("联邦基金利率目标上限", 5, rate_channel="inverse"),
    "umich_sentiment": Indicator("密歇根大学消费者信心指数", 3, "点"),
}
FAMILIES = {
    "cpi": Family("US", "bls", ("cpi_mom", "cpi_yoy", "core_cpi_mom", "core_cpi_yoy")),
    "ppi": Family("US", "bls", ("ppi_mom", "ppi_yoy")),
    "jobs": Family("US", "bls", ("payrolls", "unemployment")),
    "pce": Family("US", "bea", ("pce_mom", "pce_yoy", "core_pce_mom", "core_pce_yoy")),
    "gdp": Family("US", "bea", ("gdp",)),
    "fomc": Family("US", "fed", ("fed_rate",)),
    "umich": Family("US", "umich", ("umich_sentiment",)),
}
STAGES = {"preliminary": "初值", "final": "终值", "initial": "首发值", "updated": "更新估计", "advance": "初值", "second": "二次估计", "third": "终值"}
STAGES["flash"] = "快报值"

# 欧洲指标保留自己的口径，不复用美国年化 GDP 或美国通胀的方向标签。
INDICATORS.update({
    "eu_cpi_mom": Indicator("CPI 月率", 4),
    "eu_cpi_yoy": Indicator("CPI 年率", 4),
    "eu_core_cpi_yoy": Indicator("核心 CPI 年率", 4),
    "hicp_mom": Indicator("HICP 月率", 4),
    "hicp_yoy": Indicator("HICP 年率", 4),
    "core_hicp_yoy": Indicator("核心 HICP 年率", 4),
    "gdp_qoq": Indicator("实际 GDP 季率", 4),
    "gdp_yoy": Indicator("实际 GDP 年率", 4),
    "ilo_unemployment": Indicator("ILO 失业率", 3),
    "uk_unemployment": Indicator("ILO 失业率（三个月）", 3),
    "ecb_deposit": Indicator("存款便利利率", 5),
    "ecb_main": Indicator("主要再融资利率", 5),
    "ecb_marginal": Indicator("边际贷款利率", 5),
    "bank_rate": Indicator("英国央行基准利率", 5),
})
FAMILIES.update({
    "ecb_rate": Family("EA", "ecb", ("ecb_deposit", "ecb_main", "ecb_marginal")),
    "ea_hicp": Family("EA", "eurostat", ("hicp_yoy", "hicp_mom", "core_hicp_yoy")),
    "ea_gdp": Family("EA", "eurostat", ("gdp_qoq", "gdp_yoy")),
    "ea_jobs": Family("EA", "eurostat", ("ilo_unemployment",)),
    "boe_rate": Family("GB", "boe", ("bank_rate",)),
    "uk_cpi": Family("GB", "ons", ("eu_cpi_yoy", "eu_cpi_mom", "eu_core_cpi_yoy")),
    "uk_gdp": Family("GB", "ons", ("gdp_qoq", "gdp_yoy")),
    "uk_jobs": Family("GB", "ons", ("uk_unemployment",)),
    "de_cpi": Family("DE", "destatis", ("eu_cpi_yoy", "eu_cpi_mom", "hicp_yoy", "hicp_mom")),
    "de_gdp": Family("DE", "destatis", ("gdp_qoq", "gdp_yoy")),
    "de_jobs": Family("DE", "destatis", ("ilo_unemployment",)),
    "fr_cpi": Family("FR", "insee", ("eu_cpi_yoy", "eu_cpi_mom", "hicp_yoy", "hicp_mom")),
    "fr_gdp": Family("FR", "insee", ("gdp_qoq", "gdp_yoy")),
    "fr_jobs": Family("FR", "insee", ("ilo_unemployment",)),
})
UMICH_HOME = "https://www.sca.isr.umich.edu/"
UMICH_HISTORY = "https://data.sca.isr.umich.edu/technical-docs.php"
UMICH_INFO = "https://data.sca.isr.umich.edu/survey-info.php"
UMICH_CSV = "https://www.sca.isr.umich.edu/files/tbmics.csv"
SCHEDULE_URLS = {
    "bls": "https://www.bls.gov/schedule/news_release/bls.ics",
    "bea": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics",
    "fed": "https://www.federalreserve.gov/newsevents/calendar.htm",
}
