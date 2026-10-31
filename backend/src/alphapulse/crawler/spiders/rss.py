"""国外媒体与官方公告：只保存公开 RSS 内容，不把摘要当成文章全文。"""

import hashlib
import re
import ssl
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import escape
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from scrapy.crawler import Crawler
from scrapy.http import Request, Response
from scrapy.settings.default_settings import RETRY_EXCEPTIONS

from alphapulse.crawler.spiders.base import NewsSpider, html_to_text

ATOM = "{http://www.w3.org/2005/Atom}"


class RssNewsSpider(NewsSpider):
    feeds: tuple[str, ...]
    request_headers = {"Accept": "application/rss+xml, application/xml, text/xml", "Accept-Language": "en-US,en;q=0.9"}

    def first_requests(self):
        return [self.request(url, self.parse_feed) for url in self.feeds]

    def parse_feed(self, response: Response):
        root = ElementTree.fromstring(response.body)
        if root.tag == "rss" and root.find("channel") is not None:
            entries = root.findall("./channel/item")
        elif root.tag == f"{ATOM}feed":
            entries = root.findall(f"{ATOM}entry")
        else:
            raise ValueError("响应不是 RSS/Atom 新闻订阅，无法解析")
        for entry in entries:
            try:
                yield self._parse_entry(response, entry)
            except ValueError as exc:
                # 单条外部数据损坏不应阻止其他新闻入库，但本轮仍记为失败。
                message = f"RSS 条目解析失败（{response.url}）：{exc}"
                self.run.errors.append(message)
                self.logger.error(message)

    def _parse_entry(self, response: Response, entry: ElementTree.Element) -> dict:
        if entry.tag == f"{ATOM}entry":
            title = (entry.findtext(f"{ATOM}title") or "").strip()
            link = entry.find(f"{ATOM}link")
            url = (link.get("href", "") if link is not None else "").strip()
            date = entry.findtext(f"{ATOM}published") or ""
            published_at = datetime.fromisoformat(date)
            updated = entry.findtext(f"{ATOM}updated")
            source_updated_at = datetime.fromisoformat(updated) if updated else None
            guid = (entry.findtext(f"{ATOM}id") or "").strip() or url
            # BLS 的 Atom content 是纯文本，先转义再交给既有 HTML 展示链路。
            content_html = escape(entry.findtext(f"{ATOM}content") or "")
            author = entry.findtext(f"{ATOM}author/{ATOM}name")
            tags = [node.text.strip() for node in entry.findall(f"{ATOM}category") if node.text and node.text.strip()]
        else:
            title = html_to_text(entry.findtext("title") or "")
            url = (entry.findtext("link") or "").strip()
            date = entry.findtext("pubDate") or ""
            published_at = parsedate_to_datetime(date)
            source_updated_at = None
            guid = (entry.findtext("guid") or "").strip() or url
            content_html = entry.findtext("description") or ""
            author = entry.findtext("{http://purl.org/dc/elements/1.1/}creator")
            tags = [node.text.strip() for node in entry.findall("category") if node.text and node.text.strip()]
        # BEA 的 2025 年四季度 GDP 条目缺少 URL 协议，仅适配这一来源的官方域名。
        if self.name == "bea" and url.startswith("www.bea.gov/"):
            url = f"https://{url}"
        parsed_url = urlparse(url)
        if not title or parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ValueError("条目缺少标题或有效原文链接")
        if published_at.tzinfo is None or (source_updated_at is not None and source_updated_at.tzinfo is None):
            raise ValueError("条目发布时间或修订时间缺少时区")
        # GUID 常为长网址，哈希后满足 external_id 的 64 字符契约；不使用标题或发布时间作身份。
        return self.item(
            response,
            external_id=hashlib.sha256(guid.encode()).hexdigest(),
            url=url,
            title=title,
            summary=html_to_text(content_html),
            content_html=content_html,
            published_at=published_at.astimezone(UTC),
            source_updated_at=source_updated_at.astimezone(UTC) if source_updated_at is not None else None,
            author=author,
            tags=tags,
            extra={"language": "en", "content_scope": "feed_summary", "feed_url": response.url},
        )


class CoinDeskSpider(RssNewsSpider):
    name = "coindesk"
    feeds = ("https://www.coindesk.com/arc/outboundfeeds/rss/",)


class FedSpider(RssNewsSpider):
    name = "fed"
    feeds = (
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml",
    )


class BlsSpider(RssNewsSpider):
    name = "bls"
    # 本机 Scrapy 默认下载器访问 BLS 返回 403，已有 httpx 客户端可正常取回官方 Atom。
    # 固定使用这一下载器，不在 403 后切换客户端或叠加重试。
    custom_settings = {
        "DOWNLOAD_HANDLERS": {"https": "alphapulse.crawler.spiders.rss.BlsDownloadHandler"},
        "RETRY_EXCEPTIONS": [*RETRY_EXCEPTIONS, httpx.TransportError],
    }
    feeds = (
        "https://www.bls.gov/feed/cpi.rss",
        "https://www.bls.gov/feed/ppi.rss",
        "https://www.bls.gov/feed/empsit.rss",
    )

    def _parse_entry(self, response: Response, entry: ElementTree.Element) -> dict:
        item = super()._parse_entry(response, entry)
        release = re.fullmatch(r"/news\.release/archives/(cpi|ppi|empsit)_(\d{8})\.htm", urlparse(item["url"]).path)
        if release is None:
            raise ValueError("BLS 条目缺少可识别的公告日期，无法确定正式发布时间")
        # 这三个订阅的官方发布安排均为 08:30 ET；Atom 时间早约 40 分钟，不能用作事件时点。
        # 日期取公告归档 URL，夏令时由 America/New_York 处理，不把采集时间当发布时间。
        # 官方安排：https://www.bls.gov/schedule/news_release/{cpi,ppi,empsit}.htm
        day = datetime.strptime(release[2], "%m%d%Y")
        item["extra"]["feed_published_at"] = item["published_at"].isoformat()
        item["extra"]["published_at_basis"] = "bls_release_schedule"
        item["published_at"] = day.replace(hour=8, minute=30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
        return item


class BeaSpider(RssNewsSpider):
    name = "bea"
    feeds = ("https://apps.bea.gov/rss/rss.xml",)


class BlsDownloadHandler:
    """BLS 的公开 GET 订阅使用已有 httpx，状态码、解压和重试仍交给 Scrapy。"""

    lazy = True

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        # BLS 对容器内 Python 的默认 TLS 配置返回 403，使用与宿主一致的系统默认套件后可取回。
        # 仅调整这个来源的密码套件，保留证书/主机名校验、TLS 1.2 下限和系统安全等级。
        self.ssl_context.set_ciphers("DEFAULT")

    @classmethod
    def from_crawler(cls, crawler: Crawler):
        return cls(crawler.settings.getfloat("DOWNLOAD_TIMEOUT"))

    async def close(self) -> None:
        # Scrapy 要求关闭钩子；各请求的客户端已由上下文管理器释放。
        pass

    async def download_request(self, request: Request) -> Response:
        headers = [(name, value) for name in request.headers for value in request.headers.getlist(name)]
        async with httpx.AsyncClient(verify=self.ssl_context, timeout=request.meta.get("download_timeout", self.timeout)) as client:
            async with client.stream("GET", request.url, headers=headers) as response:
                # 保留原始压缩体，避免 httpx 与 Scrapy 的 HttpCompressionMiddleware 重复解压。
                body = b"".join([chunk async for chunk in response.aiter_raw()])
                return Response(
                    str(response.url), status=response.status_code, headers=response.headers.multi_items(),
                    body=body, request=request,
                )


class CustomRssSpider(RssNewsSpider):
    """Load administrator-managed subscriptions without registering new spider classes."""
    name = 'custom_rss'

    def __init__(self, source_code, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not re.fullmatch(r'rss_[0-9a-f]{24}', source_code):
            raise ValueError('订阅来源标识无效')
        self.name = source_code

    async def start(self):
        from alphapulse.core.db import SessionLocal
        from alphapulse.models import Source
        from alphapulse.news_risk.research import public_address
        async with SessionLocal() as session:
            source = await session.get(Source, self.name)
            if source is None or source.adapter != 'rss': raise ValueError('订阅不存在')
            await public_address(source.list_url)
            self.feeds = (source.list_url,)
        async for request in super().start(): yield request

    custom_settings = {'DOWNLOAD_HANDLERS': {'http': 'alphapulse.crawler.spiders.rss.PublicFeedDownloadHandler',
        'https': 'alphapulse.crawler.spiders.rss.PublicFeedDownloadHandler'}}

    def _parse_entry(self, response, entry):
        if entry.tag == ATOM+'entry':
            # Prefer the article link over Atom's optional self/edit links.
            links = entry.findall(ATOM+'link')
            article_link = next((link for link in links if link.get('rel', 'alternate') == 'alternate'), None)
            if article_link is not None:
                entry.remove(article_link)
                entry.insert(0, article_link)
        item = super()._parse_entry(response, entry)
        full_content = False
        if entry.tag == ATOM+'entry':
            content = entry.find(ATOM+'content')
            full_content = content is not None
            if content is None:
                content = entry.find(ATOM+'summary')
            if content is not None:
                if content.get('type') == 'xhtml':
                    body = ''.join(ElementTree.tostring(child, encoding='unicode') for child in content)
                elif content.get('type') == 'html': body = content.text or ''
                else: body = escape(content.text or '')
            else: body = item['content_html']
            item['tags'] = [node.get('term') or node.text.strip() for node in entry.findall(ATOM+'category') if node.get('term') or (node.text and node.text.strip())]
        else:
            encoded = entry.findtext('{http://purl.org/rss/1.0/modules/content/}encoded')
            full_content = bool(encoded)
            body = encoded or item['content_html']
        item.update(content_html=body, content_text=html_to_text(body), summary=html_to_text(body))
        item['extra']['language'] = 'und'
        item['extra']['content_scope'] = 'feed_content' if full_content else 'feed_summary'
        return item


class PublicFeedDownloadHandler:
    lazy = True

    @classmethod
    def from_crawler(cls, crawler): return cls()

    async def close(self): pass

    async def download_request(self, request):
        from alphapulse.news_risk.research import fetch_public_response
        response = await fetch_public_response(request.url)
        return Response(str(response.url), status=response.status_code, body=response.content, request=request,
            headers=response.headers.multi_items())
