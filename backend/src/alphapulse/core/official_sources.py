"""欧洲官方公开订阅和地域；新闻与日历共享来源身份，不共享运行状态。"""
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

EUROSTAT_NEWS = "https://ec.europa.eu/eurostat/news/euro-indicators"
EUROSTAT_FEED = EUROSTAT_NEWS + "?p_p_id=estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK&p_p_lifecycle=2&p_p_resource_id=atom&_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_pageSize=100&_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_collection=CAT_PREREL"
ONS_FEED = "https://www.ons.gov.uk/releasecalendar?rss&highlight=true&limit=100&page=1&release-type=type-published&sort=date-newest"
FEEDS = {
    "ecb": ("https://www.ecb.europa.eu/rss/press.html",),
    "eurostat": (EUROSTAT_FEED,),
    "boe": ("https://www.bankofengland.co.uk/rss/news", "https://www.bankofengland.co.uk/rss/speeches"),
    "ons": (ONS_FEED,),
    "destatis": ("https://www.destatis.de/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeed/Aktuell.xml",),
    "insee": ("https://www.insee.fr/fr/flux/3",),
}
REGIONS = {"wscn_news": "CN", "wscn_live": "CN", "cls_depth": "CN", "cls_telegraph": "CN", "coindesk": "GLOBAL",
           "bls": "US", "bea": "US", "fed": "US", "umich": "US", "dol": "US", "eia": "US", "adp": "US", "ecb": "EA", "eurostat": "EA", "boe": "GB", "ons": "GB", "destatis": "DE", "insee": "FR"}
HOSTS = {"ecb": {"www.ecb.europa.eu"}, "eurostat": {"ec.europa.eu"}, "boe": {"www.bankofengland.co.uk"},
         "ons": {"www.ons.gov.uk"}, "destatis": {"www.destatis.de"}, "insee": {"www.insee.fr"}}
LANGUAGES = {"destatis": "de", "insee": "fr"}
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class Entry:
    title: str
    url: str
    at: datetime
    summary: str
    updated: datetime | None = None

    @property
    def identity(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()


def official_url(source: str, url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS[source]:
        raise ValueError(f"{source} 链接不是对应机构的官方 HTTPS 地址")
    # ECB 的公开 RSS 链接带重复路径分隔符；身份按同一官方规范地址去重。
    if source == "ecb":
        url = parsed._replace(path=re.sub(r"/+", "/", parsed.path)).geturl()
    return url


def feed_entries(body: bytes, source: str) -> list[Entry]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("官方订阅不是有效的 XML") from exc
    if root.tag == "rss" and root.find("channel") is not None:
        nodes = root.findall("./channel/item")
    elif root.tag == ATOM + "feed":
        nodes = root.findall(ATOM + "entry")
    else:
        raise ValueError("官方响应不是 RSS/Atom 订阅")
    result = []
    for node in nodes:
        if node.tag == ATOM + "entry":
            title = node.findtext(ATOM + "title", "").strip()
            link = next((n for n in node.findall(ATOM + "link") if n.get("rel", "alternate") == "alternate"), None)
            url = link.get("href", "") if link is not None else ""
            at = datetime.fromisoformat(node.findtext(ATOM + "published") or node.findtext(ATOM + "updated") or "")
            updated = node.findtext(ATOM + "updated")
            summary = node.findtext(ATOM + "summary") or node.findtext(ATOM + "content") or ""
        else:
            title = node.findtext("title", "").strip()
            url = node.findtext("link", "").strip()
            at = parsedate_to_datetime(node.findtext("pubDate", ""))
            updated = None
            summary = node.findtext("description", "")
        updated_at = datetime.fromisoformat(updated) if updated else None
        if not title or at.tzinfo is None or (updated_at is not None and updated_at.tzinfo is None):
            raise ValueError("官方订阅缺少标题或带时区的发布/修订时间")
        result.append(Entry(title, official_url(source, url), at.astimezone(UTC), summary,
                            updated_at.astimezone(UTC) if updated_at else None))
    return result


def relevant_news(source: str, title: str, url: str) -> bool:
    title = title.casefold()
    if source == "ecb":
        return any(p in url for p in ("/key/", "/inter/", "/accounts/", "/pressconf/")) or "monetary policy decisions" in title
    if source == "boe":
        return "/speech/" in url or "/monetary-policy-summary-and-minutes/" in url or "bank rate" in title or "monetary policy" in title
    if source == "eurostat":
        return "inflation" in title or "unemployment" in title or bool(re.search(r"\bgdp\b.*(?:up|down|stable|growth|increased|decreased)|(?:growth|increase|decrease).*\bgdp\b", title))
    if source == "ons":
        return "time series" not in title and not url.endswith("timeseries") and bool(re.match(r"(?:consumer price inflation, uk:|labour market overview, uk:|uk labour market(?::|\s)|gdp first quarterly estimate, uk:|gdp quarterly national accounts, uk:)", title))
    if source == "destatis":
        return any(t in title for t in ("verbraucherpreis", "inflationsrate", "bruttoinlandsprodukt", "erwerbstätig", "erwerbslos"))
    if source == "insee":
        return any(t in title for t in ("prix à la consommation", "taux de chômage", "le pib", "le produit intérieur brut"))
    raise ValueError(f"未支持的欧洲新闻来源：{source}")
