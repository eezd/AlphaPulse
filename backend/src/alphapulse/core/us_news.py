"""Official US news catalog and deterministic collection policy."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from parsel import Selector


@dataclass(frozen=True)
class OfficialSource:
    name: str
    entries: tuple[str, ...]
    article_path: str
    body: str
    hosts: tuple[str, ...]
    interval: int = 300


SOURCES = {
    'sec': OfficialSource('SEC·证券监管', ('https://www.sec.gov/newsroom/press-releases', 'https://www.sec.gov/newsroom/speeches-statements', 'https://www.sec.gov/rules-regulations/rulemaking-activity', 'https://www.sec.gov/rules-regulations/self-regulatory-organization-rulemaking'), r'/(?:newsroom/(?:press-releases|speeches-statements)/[^/]+|rules-regulations/.*\d.*)', '.node-details-layout__main-region__content .field--name-body', ('www.sec.gov',)),
    'cftc': OfficialSource('CFTC·衍生品监管', ('https://www.cftc.gov/PressRoom/PressReleases', 'https://www.cftc.gov/PressRoom/SpeechesTestimony', 'https://www.cftc.gov/LawRegulation/CFTCStaffLetters/letters.htm'), r'/(?:PressRoom/(?:PressReleases/\d+-\d+|SpeechesTestimony/(?!index)[^/]+$)|csl/)', 'article .field--name-body', ('www.cftc.gov',)),
    'treasury': OfficialSource('美国财政部', ('https://home.treasury.gov/news/press-releases',), r'/news/press-releases/[a-z]+\d+/?$', '.field--name-field-news-body', ('home.treasury.gov', 'www.treasury.gov', 'ofac.treasury.gov')),
    'ofac': OfficialSource('OFAC·金融制裁', ('https://ofac.treasury.gov/recent-actions',), r'/recent-actions/\d{8}[^/]*$', 'article.node--type-ofac-recent-action .field--name-field-body', ('ofac.treasury.gov', 'home.treasury.gov')),
    'fincen': OfficialSource('FinCEN·反洗钱', ('https://www.fincen.gov/news/press-releases', 'https://www.fincen.gov/news/enforcement-actions', 'https://www.fincen.gov/resources/advisoriesbulletinsfact-sheets'), r'/(?:news/news-releases/[^/]+|resources/advisories/[^/]+|news/enforcement-actions/[^/]+)', 'article .field--name-body', ('www.fincen.gov',)),
    'whitehouse': OfficialSource('白宫·政策', ('https://www.whitehouse.gov/presidential-actions/', 'https://www.whitehouse.gov/fact-sheets/', 'https://www.whitehouse.gov/briefings-statements/', 'https://bidenwhitehouse.archives.gov/briefing-room/'), r'/(?:presidential-actions|fact-sheets|briefings-statements|briefing-room/[^/]+)/202[5-9]/\d{2}/\d{2}/|/(?:presidential-actions|fact-sheets|briefings-statements)/202[5-9]/\d{2}/[^/]+', '.wp-block-post-content, .body-content, .entry-content', ('www.whitehouse.gov', 'bidenwhitehouse.archives.gov')),
    'congress': OfficialSource('美国国会·立法', ('https://api.congress.gov/v3/bill?format=json&limit=250', 'https://api.congress.gov/v3/committee-meeting?format=json&limit=250'), r'/bill/', '', ('api.congress.gov', 'www.congress.gov', 'www.govinfo.gov'), 900),
    'federal_register': OfficialSource('Federal Register·规则', ('https://www.federalregister.gov/api/v1/documents.json',), r'/documents/', '', ('www.federalregister.gov', 'public-inspection.federalregister.gov', 'www.govinfo.gov'), 900),
    'occ': OfficialSource('OCC·银行与稳定币', tuple(f'https://www.occ.gov/news-events/newsroom/news-issuances-by-year/{kind}/{year}-{kind}.html' for year in range(datetime.now(UTC).year, 2024, -1) for kind in ('news-releases', 'bulletins')), r'/news-issuances/(?:news-releases|bulletins|interpretations-and-precedents)/202[5-9]/[^/]+', '.occgov-issuance-content', ('www.occ.gov', 'occ.gov', 'www.occ.treas.gov', 'occ.treas.gov')),
    'doj': OfficialSource('DOJ·加密机构执法', ('https://www.justice.gov/api/v1/press_releases.json?pagesize=50&sort=date&direction=DESC&fields=uuid,date',), r'/(?:opa|usao[^/]*|archives/opa)/(?:pr|speech)/[^/]+', '.field--name-body, .field--name-field-pr-body', ('www.justice.gov',)),
    'fed': OfficialSource('美联储·政策与讲话', tuple(f'https://www.federalreserve.gov/newsevents/{kind}/{year}-{suffix}.htm' for year in range(datetime.now(UTC).year, 2024, -1) for kind, suffix in (('pressreleases', 'press'), ('speech', 'speeches'), ('testimony', 'testimony'))) + ('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm', 'https://www.federalreserve.gov/releases/h41/releaseDates.json'), r'/(?:newsevents/(?:pressreleases|speech|testimony)/[^/]+\.htm|monetarypolicy/(?:fomcminutes|fomcprojtabl)\d+\.htm|releases/h41/\d+/)', '#article, .col-xs-12.col-sm-8.col-md-8, #content', ('www.federalreserve.gov',), 60),
}
RULE_VERSION = 'us-policy-v2'
CRYPTO = re.compile(r'\b(?:crypto(?:currency|currencies|[ -]assets?)?|digital[ -]assets?|virtual[ -]currenc(?:y|ies)|stablecoins?|bitcoin|ethereum|blockchain|tokeniz(?:ed|ation))\b', re.I)
POLICY = re.compile(r'\b(?:regulat\w*|rules?|guidance|legislation|acts?|sanctions?|licenses?|executive order|monetary policy|federal funds|interest rates?|balance sheet|liquidity|quantitative|refunding|borrowing|debt limit|tariffs?|fiscal|budget|appropriation|tax reform)\b', re.I)
INSTITUTION = re.compile(r'\b(?:exchanges?|banks?|financial institutions?|payment processors?|custodians?|stablecoin issuers?|binance|coinbase|tether|circle|kraken|bybit|bitfinex)\b', re.I)
MACRO = re.compile(r"\b(?:monetary policy|federal funds (?:rate|target)|interest rates?|discount rates?|(?:primary|secondary) credit rates?|reserve requirements?|interest on reserve balances|fomc|balance sheet|liquidity|quantitative (?:easing|tightening)|refunding|borrowing estimates|debt (?:limit|ceiling)|tariffs?|fiscal (?:policy|stimulus|outlook)|federal (?:budget|deficit)|president['’]s budget|budget of the united states|(?:continuing|consolidated|full[ -]year) appropriations?|government (?:shutdown|funding)|tax reform|financial sanctions)\b", re.I)


def selection(source: str, title: str, text: str) -> list[str]:
    content = f'{title}\n{text}'
    crypto = bool(CRYPTO.search(content))
    if crypto and (source not in ('doj', 'ofac', 'fincen') or POLICY.search(title) or INSTITUTION.search(content)):
        return ['crypto_policy' if POLICY.search(content) else 'crypto_institution']
    macro_scope = title if source == 'federal_register' else content
    if source not in ('doj', 'cftc', 'sec') and MACRO.search(macro_scope):
        return ['us_macro_policy']
    if source == 'ofac' and INSTITUTION.search(content) and POLICY.search(content):
        return ['financial_sanctions']
    return []


def official_url(source: str, url: str, base: str = '') -> str:
    value = urlsplit(urljoin(base, url))
    if value.scheme != 'https' or value.hostname not in SOURCES[source].hosts or value.username or value.password:
        raise ValueError('链接不是该来源的官方 HTTPS 地址')
    if re.search(r'(?:api_key|token|key)=', value.query, re.I):
        raise ValueError('官方链接含凭据参数')
    return urlunsplit((value.scheme, value.netloc, value.path, value.query, ''))


def identity(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def published(value: str) -> tuple[datetime, str]:
    if not isinstance(value, str):
        raise ValueError('官方材料缺少可识别的发布时间')
    value = value.strip().replace('\xa0', ' ')
    value = re.sub(r'\bSept(?=\.? \d)', 'Sep', value)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return datetime.fromisoformat(value).replace(tzinfo=UTC), 'date'
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo:
            return dt.astimezone(UTC), 'datetime'
    except ValueError:
        pass
    match = re.search(r'([A-Z][a-z]+\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4})', value)
    if match:
        for fmt in ('%B %d, %Y', '%b %d, %Y', '%m/%d/%Y'):
            try:
                return datetime.strptime(match[0].replace('.', ''), fmt).replace(tzinfo=UTC), 'date'
            except ValueError:
                pass
    raise ValueError('官方材料缺少可识别的发布时间')


def validate_body(content: str) -> None:
    """Reject error documents without requiring full-page HTML around fragments."""
    if content.lstrip().startswith('<?xml'):
        from lxml import etree
        try:
            etree.fromstring(content.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
        except etree.XMLSyntaxError as exc:
            raise ValueError('官方 XML 正文无法解析') from exc
    sel = Selector(content)
    heading = ' '.join(sel.css('title, h1').xpath('normalize-space(.)').getall())
    text = ' '.join(sel.xpath('//text()[not(ancestor::script or ancestor::style)]').getall()).strip()
    if re.search(r'just a moment|access denied|verify (?:you are|your)|sign in|log in|captcha|page not found|service unavailable|bad gateway', heading, re.I) or sel.css('#challenge-form, #cf-challenge-running, script[src*="/cdn-cgi/challenge"]'):
        raise ValueError('官方正文返回验证或登录页面')
    if re.search(r'enable javascript and cookies to continue', text, re.I):
        raise ValueError('官方正文返回验证页面')
    if len(text) < 40:
        raise ValueError('官方正文为空或过短')


def article(source: str, html: str, url: str, *, release_date: str | None = None) -> dict:
    sel = Selector(html)
    validate_body(html)
    selectors = {
        'fed': ('#article h3.title', '#article h3', '#article h2', '#content .heading h3', '#content .col-xs-12.col-sm-8.col-md-8 h3'),
        'treasury': ('h2.uswds-page-title',),
        'cftc': ('article h1:not(.press-release-title)',),
    }.get(source, ('.main-content__main h1', '#main_content h1', 'main h1', 'h1'))
    title = next((value for css in selectors if (value := sel.css(css).xpath('normalize-space(.)').get())), None)
    if source == 'fed' and '/releases/h41/' in url:
        title = 'Federal Reserve Balance Sheet — H.4.1'
    if not title:
        raise ValueError('官方正文缺少标题')
    values = sel.css('meta[property="article:published_time"]::attr(content), meta[name="DC.date.issued"]::attr(content), meta[name="dcterms.date"]::attr(content), time::attr(datetime)').getall()
    if source == 'sec':
        values += sel.css('.field--name-dynamic-twig-fieldnode-press-release-lead-in').xpath('normalize-space(.)').getall()
    for script in sel.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(script)
            nodes = data if isinstance(data, list) else data.get('@graph', [data])
            values += [n['datePublished'] for n in nodes if isinstance(n, dict) and n.get('datePublished')]
        except (ValueError, TypeError, AttributeError):
            continue
    values += sel.css('.node-details-layout__main-region__content, .date, .date-display-single, .release-date, .article__time, .published-date, .field--name-field-date, .field--name-field-release-date, .press-release-date').xpath('normalize-space(.)').getall()
    values += re.findall(r'(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}', ' '.join(sel.css('main, article, #content').xpath('string(.)').getall()))[:3]
    # OFAC action identifiers encode the publisher's day, not the fetch time.
    if source == 'fed' and (m := re.search(r'/releases/h41/(\d{4})(\d{2})(\d{2})', url)):
        values.insert(0, '-'.join(m.groups()))
    if source == 'ofac' and (m := re.search(r'/recent-actions/(\d{4})(\d{2})(\d{2})', url)):
        values.insert(0, '-'.join(m.groups()))
    if source == 'fed' and '/monetarypolicy/fomcminutes' in url:
        if release_date is None:
            raise ValueError('FOMC 纪要缺少官方日历发布日期')
        values = [release_date]
    at = precision = None
    for value in values:
        try:
            at, precision = published(value)
            break
        except ValueError:
            continue
    if at is None:
        raise ValueError('官方材料缺少可识别的发布时间')
    body = sel.css(SOURCES[source].body)
    if not body:
        raise ValueError('官方正文结构未识别')
    for node in body.css('script, style, nav, header, footer, form'):
        parent = node.root.getparent()
        if parent is not None:
            parent.remove(node.root)
    body = sorted(body, key=lambda node: len(node.xpath('string(.)').get()), reverse=True)
    content = body[0].get()
    text = ' '.join(body[0].xpath('.//text()').getall()).strip()
    if len(text) < 40:
        raise ValueError('官方正文为空或过短')
    attachments = []
    for link in body[0].css('a[href]'):
        href = link.attrib['href']
        label = link.xpath('normalize-space(.)').get()
        if re.search(r'\.pdf(?:\?|$)|/media/\d+/(?:download|dl)|/csl/[^/]+/download', href, re.I):
            try:
                link_url = official_url(source, href, url)
            except ValueError:
                candidate = urljoin(url, href)
                if urlsplit(candidate).scheme == 'https':
                    attachments.append({'url': candidate, 'title': label, 'status': 'unsupported_host', 'error': '附件不在该来源的官方域名范围内，未下载'})
                continue
            if link_url not in [a['url'] for a in attachments]:
                attachments.append({'url': link_url, 'title': label, 'status': 'pending'})
    return dict(title=title, content_html=content, published_at=at,
                extra={'language': 'en', 'content_scope': 'article', 'published_at_precision': precision,
                       'published_date': at.date().isoformat(), 'rule_version': RULE_VERSION, 'attachments': attachments})
