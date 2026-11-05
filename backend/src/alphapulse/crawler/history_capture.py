"""可恢复历史摘要导出；输出真实响应供既有 import_history 导入，不写业务表。"""
import argparse
import asyncio
import base64
import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from parsel import Selector
from sqlalchemy import select

from alphapulse.calendar.backfill import fetch_documents, family_for
from alphapulse.calendar.archives import parse_announcements, publication_time
from alphapulse.crawler.europe_history import capture as european_capture
from alphapulse.crawler.import_history import ArchivedArticle, Capture
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.official_sources import FEEDS, relevant_news
from alphapulse.models import RawBody, RawResponse

SOURCES = ('coindesk', 'bls', 'bea', *FEEDS)


def timeline_articles(data):
    if not isinstance(data.get('articles'), list) or not isinstance(data.get('hasMore'), bool):
        raise ValueError('CoinDesk 时间线结构未识别')
    return [ArchivedArticle(guid=a['_id'], url=urljoin('https://www.coindesk.com', a['pathname']),
                            title=a['title'], summary=a.get('description') or '',
                            published_at=a['articleDates']['displayDate']) for a in data['articles']]


def ecb_articles(data):
    if not isinstance(data, list) or len(data) % 13:
        raise ValueError('ECB 官方索引记录宽度已变化')
    articles = []
    for i in range(0, len(data), 13):
        row = data[i:i + 13]
        at = datetime.fromtimestamp(row[1], UTC)
        for path in row[9]:
            url = urljoin('https://www.ecb.europa.eu', path)
            title = row[10]['Title']
            if relevant_news('ecb', title, url) and not url.endswith('.pdf'):
                articles.append(ArchivedArticle(guid=url, url=url, title=title, summary='', published_at=at))
    return articles


def package(source, url, body, at, headers, articles):
    return Capture(source=source, url=url, status=200, headers=headers,
                   body=base64.b64encode(body).decode(), body_encoding='base64',
                   fetched_at=at, articles=articles)


async def export(source, begin, end, output):
    errors, captures = [], []
    existing = []
    async with SessionLocal() as session:
        rows = (await session.execute(select(RawResponse, RawBody.body).join(
            RawBody, RawBody.sha256 == RawResponse.body_sha256).where(
                RawResponse.source_code == source, RawResponse.status == 200,
                RawResponse.url.contains('/chunk_') if source == 'ecb' else
                RawResponse.url.contains('/api/v1/articles/timeline'))
            .distinct(RawResponse.url, RawResponse.body_sha256)
            .order_by(RawResponse.url, RawResponse.body_sha256, RawResponse.id))).all() if source in ('ecb', 'coindesk') else []
        for raw, body in rows:
            body = gzip.decompress(body)
            try:
                articles = ecb_articles(json.loads(body)) if source == 'ecb' else timeline_articles(json.loads(body))
                captures.append(package(source, raw.url, body, raw.fetched_at, raw.headers, articles))
                existing.append(raw.url)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                errors.append({'url': raw.url, 'error': str(exc)})
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        async def get(url, host):
            await asyncio.sleep(1)
            for _ in range(6):
                parsed = urlparse(url)
                if parsed.scheme != 'https' or parsed.hostname != host:
                    raise ValueError('归档链接或重定向不属于预期官方域名')
                r = await client.get(url, headers={'User-Agent': 'AlphaPulse historical news collector'})
                if not r.is_redirect:
                    break
                url = urljoin(url, r.headers['location'])
            else:
                raise ValueError('归档重定向超过五次')
            r.raise_for_status()
            return r
        try:
            if source == 'coindesk':
                url, visited = 'https://www.coindesk.com/api/v1/articles/timeline?size=16&lang=en', set()
                while url:
                    if url in visited:
                        raise ValueError('CoinDesk 历史分页形成循环')
                    visited.add(url)
                    r = await get(url, 'www.coindesk.com'); data = r.json()
                    articles = timeline_articles(data)
                    captures.append(package(source, str(r.url), r.content, datetime.now(UTC), dict(r.headers), articles))
                    if not articles:
                        if data['hasMore']:
                            raise ValueError('CoinDesk 空页仍声明有后续数据')
                        break
                    dates = [a.published_at for a in articles]
                    if dates != sorted(dates, reverse=True):
                        raise ValueError('CoinDesk 时间线日期非倒序')
                    if min(dates) < begin or not data['hasMore']:
                        break
                    a = data['articles'][-1]
                    url = str(httpx.URL('https://www.coindesk.com/api/v1/articles/timeline', params={
                        'size': 16, 'lang': 'en', 'lastDisplayDate': a['articleDates']['displayDate'], 'lastId': a['_id']}))
            elif source == 'bea':
                url, visited = 'https://www.bea.gov/news/archive?field_related_product_target_id=All&created_1=All&title=', set()
                while url:
                    if url in visited:
                        raise ValueError('BEA 归档分页形成循环')
                    visited.add(url)
                    r = await get(url, 'www.bea.gov'); sel = Selector(text=r.text)
                    links = list(dict.fromkeys(urljoin(str(r.url), u) for u in sel.css('a[href]::attr(href)').getall()
                        if any('/news/' + str(y) + '/' in u for y in range(begin.year, end.year + 1))))
                    for link in links:
                        page = await get(link, 'www.bea.gov'); article = Selector(text=page.text)
                        stamp = article.css('.field--name-field-release-date').xpath('string(.)').get()
                        if not stamp:
                            raise ValueError('BEA 公告缺少正式发布日期')
                        a = ArchivedArticle(guid=link, url=link, title=article.css('h1').xpath('string(.)').get(),
                            summary=article.css('meta[name="description"]::attr(content)').get() or '', published_at=publication_time(stamp))
                        if begin <= a.published_at < end:
                            captures.append(package(source, link, page.content, datetime.now(UTC), dict(page.headers), [a]))
                    following = sel.css('.pager__item--next a::attr(href)').get()
                    years = re.findall(r'/news/(20\d\d)/', r.text)
                    url = urljoin(str(r.url), following) if following and (not years or max(map(int, years)) >= begin.year) else None
            elif source == 'ecb':
                if not existing:
                    raise ValueError('ECB 缺少已验证的官方索引入口')
                for url in dict.fromkeys(existing):
                    r = await get(url, 'www.ecb.europa.eu')
                    captures.append(package(source, str(r.url), r.content, datetime.now(UTC), dict(r.headers), ecb_articles(r.json())))
                errors.append({'error': 'ECB 已存索引已重验；动态索引版本至任务终点的完整性仍未证实'})
            elif source == 'boe':
                r = await get('https://www.bankofengland.co.uk/sitemap/speeches', 'www.bankofengland.co.uk')
                links = list(dict.fromkeys(urljoin(str(r.url), p) for p in Selector(text=r.text).css('a[href]::attr(href)').getall() if '/speech/' in p))
                if not links:
                    raise ValueError('BoE 讲话索引没有可识别的链接')
                for url in links:
                    page = await get(url, 'www.bankofengland.co.uk');sel = Selector(text=page.text)
                    dates = sel.css('time::attr(datetime), meta[property="article:published_time"]::attr(content)').getall()
                    if not dates:
                        raise ValueError('BoE 讲话缺少可识别的正式日期')
                    a = ArchivedArticle(guid=url, url=url, title=sel.css('h1').xpath('string(.)').get(),
                                        summary=sel.css('.page-content p').xpath('string(.)').get() or '', published_at=dates[0])
                    if begin <= a.published_at < end:
                        captures.append(package(source, url, page.content, datetime.now(UTC), dict(page.headers), [a]))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            item = {'error': f'{type(exc).__name__}: {exc}'}
            if isinstance(exc, httpx.HTTPStatusError):
                item['retry_after'] = exc.response.headers.get('Retry-After')
            errors.append(item)
    if source != 'coindesk' and not (source in ('boe', 'ecb') and any('HTTPStatusError' in e.get('error', '') for e in errors)):
        documents = await fetch_documents(begin.date(), end.date(), set(), errors, sources={source})
        for doc in documents:
            try:
                if source in FEEDS:
                    captures.append(european_capture(doc))
                else:
                    family = family_for(doc)
                    if family:
                        announcements = parse_announcements(family, doc.body, pdf=doc.body.startswith(b'%PDF'))
                        title = Selector(text=doc.body.decode(errors='replace')).css('h1').xpath('string(.)').get() or family
                        articles = [ArchivedArticle(guid=doc.url, url=doc.url, title=title, summary='', published_at=a.at) for a in announcements]
                        captures.append(package(source, doc.url, doc.body, doc.captured_at, doc.headers, articles))
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                errors.append({'url': doc.url, 'error': str(exc)})
    with gzip.open(output, 'xt', encoding='utf-8') as stream:
        for capture in captures:
            capture.articles = [a for a in capture.articles if begin <= a.published_at < end]
            stream.write(capture.model_dump_json() + '\n')
    return {'source': source, 'responses': len(captures), 'articles': sum(len(c.articles) for c in captures),
            'errors': errors, 'output': str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', choices=SOURCES)
    parser.add_argument('--start', required=True, type=datetime.fromisoformat)
    parser.add_argument('--end', required=True, type=datetime.fromisoformat)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.start.tzinfo is None or args.end.tzinfo is None or args.start >= args.end:
        parser.error('范围须带时区且开始早于结束')
    async def run():
        try:
            return await export(args.source, args.start, args.end, args.output)
        finally:
            await engine.dispose()
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))


if __name__ == '__main__':
    main()
