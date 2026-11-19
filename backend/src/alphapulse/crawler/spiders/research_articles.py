"""六家来源的内置入口；未验证来源明确失败，不生成空成功轮次。"""
import json
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from alphapulse.core.db import SessionLocal
from alphapulse.core.research_sources import SOURCES
from alphapulse.crawler import research_articles as parser
from alphapulse.crawler.spiders.base import NewsSpider
from alphapulse.models import Source


class ResearchArticleSpider(NewsSpider):
    custom_settings = {'REDIRECT_ENABLED': False}

    def first_requests(self):
        if SOURCES[self.name].availability != 'ready':
            raise ValueError(SOURCES[self.name].unavailable_reason)
        yield self.list_request(0, history=False, pages=1)

    def history_request(self, before):
        # 启动时从持久游标恢复；普通巡检始终从首页开始。
        return self.request(SOURCES[self.name].entry_urls[0], self.start_history, history=True)

    async def start_history(self, response):
        async with SessionLocal() as session:
            cursor = await session.scalar(select(Source.history_cursor).where(Source.code == self.name))
        saved = (cursor or {}).get('history', {})
        page = saved.get('page', 0) if saved.get('target') == self.run.history_target.isoformat() else 0
        last_time = saved.get('last_time') if self.name == 'cls_depth' and page else None
        if self.name == 'cls_depth' and page and not isinstance(last_time, int):
            page = 0
        yield self.list_request(page, history=True, pages=1, cursor=last_time)

    def list_request(self, page, *, history, pages, cursor=None):
        url, payload = parser.list_request(self.name, page, cursor)
        request = self.request(url, self.parse_list, history=history, page=page, pages=pages, cursor=cursor)
        if payload is not None:
            request = request.replace(method='POST', body=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        return request

    async def parse_list(self, response):
        entries, more = parser.parse_list(self.name, response.text, response.meta['page'])
        history = response.meta['history']
        cutoff = self.run.history_target if history else self.run.cutoff
        for entry in entries:
            if self.run.blocked:
                break
            if entry['published_at'] is not None and entry['published_at'] < cutoff:
                continue
            yield self.request(entry['url'], self.parse_article, entry=entry, cutoff=cutoff,
                               handle_httpstatus_list=[301, 302, 303, 307, 308, 401, 403, 429])
        dates = [entry['published_at'] for entry in entries if entry['published_at'] is not None]
        crossed = bool(entries) and len(dates) == len(entries) and max(dates) < cutoff
        cursor = parser.cls_cursor(response.text, response.meta['page']) if self.name == 'cls_depth' else None
        if cursor and response.meta['page'] and cursor >= response.meta['cursor']:
            raise ValueError('财联社头条翻页时间游标未前进')
        if history:
            if dates:
                self.run.history_position = min(self.run.history_position, min(dates))
            self.run.history_done = not more or crossed
            checkpoint = {'page': response.meta['page'] + 1, 'target': self.run.history_target.isoformat()}
            if self.name == 'cls_depth' and cursor:
                checkpoint['last_time'] = cursor
            self.run.official_cursor = {'history': checkpoint}
        if (more and not crossed and not self.run.blocked and response.meta['pages'] < 20
                and (history or self.name != 'goldman_insights')
                and (not history or time.monotonic() < self.run.history_deadline)):
            yield self.list_request(response.meta['page'] + 1, history=history,
                                    pages=response.meta['pages'] + 1, cursor=cursor)

    async def parse_article(self, response):
        if not parser.allowed_url(self.name, response.url):
            raise ValueError('详情响应来自未允许的站点')
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get('Location')
            target = response.urljoin(location.decode()) if location else ''
            redirects = response.meta.get('research_redirects', 0)
            if not parser.allowed_url(self.name, target) or parser.file_url(target) or redirects >= 3:
                raise ValueError('文章重定向目标未允许或超过三次跳转')
            yield self.request(target, self.parse_article, entry=response.meta['entry'],
                               cutoff=response.meta['cutoff'], research_redirects=redirects + 1,
                               handle_httpstatus_list=[301, 302, 303, 307, 308, 401, 403, 429])
            return
        if response.status in (403, 429):
            self.block_until(datetime.now(UTC) + timedelta(minutes=15))
            self.run.errors.append(f'详情受限 HTTP {response.status}：{response.url}')
        item = parser.parse_article(self.name, response.url, response.text, response.meta['entry'], response.status)
        if item is None:
            return
        if not item['extra']['published_at_unreliable'] and item['published_at'] < response.meta['cutoff']:
            return
        if item['extra']['research']['content_status'] == 'extraction_failed':
            self.run.errors.append(f'文章正文解析失败：{response.url}')
        yield self.item(response, **item)


class ClsDepthSpider(ResearchArticleSpider):
    name = 'cls_depth'


class CitiInsightsSpider(ResearchArticleSpider):
    name = 'citi_insights'


class JpmorganResearchSpider(ResearchArticleSpider):
    name = 'jpmorgan_research'


class BlackrockInsightsSpider(ResearchArticleSpider):
    name = 'blackrock_insights'


class GoldmanInsightsSpider(ResearchArticleSpider):
    name = 'goldman_insights'


class BloombergArticlesSpider(ResearchArticleSpider):
    name = 'bloomberg_articles'
