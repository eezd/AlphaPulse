"""公开文章接入目录；接入验证状态不替代运行健康状态。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchSource:
    code: str
    name: str
    entry_urls: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    availability: str
    unavailable_reason: str | None = None


SOURCES = {source.code: source for source in (
    ResearchSource('cls_depth', '财联社文章', ('https://www.cls.cn/depth?id=1000',),
                   ('www.cls.cn',), 'ready'),
    ResearchSource('citi_insights', 'Citi', ('https://www.citigroup.com/global/insights/all',),
                   ('www.citigroup.com',), 'ready'),
    ResearchSource('jpmorgan_research', 'J.P. Morgan', ('https://www.jpmorgan.com/insights/research',),
                   ('www.jpmorgan.com',), 'ready'),
    ResearchSource('blackrock_insights', 'BlackRock',
                   ('https://www.blackrock.com/corporate/insights/blackrock-investment-institute',),
                   ('www.blackrock.com',), 'unverified', '公开文章与正文可访问，列表分页／完整范围尚未验证'),
    ResearchSource('goldman_insights', 'Goldman Sachs', ('https://www.goldmansachs.com/insights/articles',),
                   ('www.goldmansachs.com',), 'ready'),
    ResearchSource('bloomberg_articles', 'Bloomberg',
                   ('https://www.bloomberg.com/markets', 'https://www.bloomberg.com/economics'),
                   ('www.bloomberg.com',), 'blocked', '公开入口返回 HTTP 403；没有可验证的文章列表'),
)}
HISTORY_SOURCES = tuple(code for code, source in SOURCES.items() if source.availability == 'ready')


def configuration_error(code):
    source = SOURCES.get(code)
    return source.unavailable_reason if source else None
