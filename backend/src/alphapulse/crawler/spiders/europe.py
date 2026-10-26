"""欧洲六机构的公开宏观摘要；完整原文仍从官方链接查看。"""
from scrapy.http import Response

from alphapulse.core.official_sources import FEEDS, LANGUAGES, feed_entries, relevant_news
from alphapulse.crawler.spiders.base import NewsSpider, html_to_text


class EuropeanNewsSpider(NewsSpider):
    request_headers = {"User-Agent": "AlphaPulse official news collector", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}

    def first_requests(self):
        return [self.request(url, self.parse_feed) for url in FEEDS[self.name]]

    def parse_feed(self, response: Response):
        # 整份订阅格式破坏时让既有生命周期报告失败，不把空列表当成功。
        for entry in feed_entries(response.body, self.name):
            if relevant_news(self.name, entry.title, entry.url):
                yield self.item(response, external_id=entry.identity, url=entry.url,
                                title=html_to_text(entry.title), summary=html_to_text(entry.summary),
                                content_html=entry.summary, published_at=entry.at, source_updated_at=entry.updated,
                                extra={"language": LANGUAGES.get(self.name, "en"), "content_scope": "feed_summary", "feed_url": response.url})


class EcbSpider(EuropeanNewsSpider):
    name = "ecb"


class EurostatSpider(EuropeanNewsSpider):
    name = "eurostat"


class BoeSpider(EuropeanNewsSpider):
    name = "boe"


class OnsSpider(EuropeanNewsSpider):
    name = "ons"


class DestatisSpider(EuropeanNewsSpider):
    name = "destatis"


class InseeSpider(EuropeanNewsSpider):
    name = "insee"
