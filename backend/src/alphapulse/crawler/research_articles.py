"""已核验的公开列表和文章解析，不以文章价值筛选，不下载附件。"""
import hashlib
import json
from datetime import UTC, datetime
from html import escape
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from parsel import Selector

from alphapulse.core.research_sources import SOURCES
from alphapulse.crawler.content_policy import file_url
from alphapulse.crawler.spiders.base import html_to_text

CLS_HOME = 'https://www.cls.cn/v3/depth/home/assembled/1000'
CLS_PAGE = 'https://www.cls.cn/v3/depth/list/1000'
GOLDMAN_FEED = 'https://www.goldmansachs.com/feeds/insights.json'
GOLDMAN_PAGE_SIZE = 20
CITI_LIST = 'https://www.citigroup.com/rcs/v1/content/citigpa/search/contentTypes/insights'
JPM_LIST = ('https://www.jpmorgan.com/services/json/v1/dynamic-grid.service/'
            'parent=jpmorgan/global/US/en/insights/research&comp=root/content-parsys/dynamic_grid_copy_co&page=p{page}.json')


def canonical_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ''))


def allowed_url(code, url):
    parts = urlsplit(url)
    return parts.scheme in ('https', 'http') and parts.hostname in SOURCES[code].allowed_hosts and not parts.username


def identifier(url, external_id=None):
    return str(external_id) if external_id else hashlib.sha256(canonical_url(url).encode()).hexdigest()


def published_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        result = datetime.strptime(value.strip(), '%B %d, %Y')
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def _cls_rows(data, page):
    if data.get('errno') != 0:
        raise ValueError('财联社头条列表请求失败')
    payload = data['data']
    rows = payload['depth_list'] if page == 0 else payload
    if not isinstance(rows, list):
        raise ValueError('财联社头条列表结构无效')
    return rows


def cls_cursor(body, page):
    rows = _cls_rows(json.loads(body), page)
    return rows[-1]['ctime'] if rows else None


def list_request(code, page, cursor=None):
    if code == 'cls_depth':
        params = {'app': 'CailianpressWeb', 'os': 'web', 'sv': '8.7.9'}
        if page:
            if not isinstance(cursor, int) or cursor <= 0:
                raise ValueError('财联社翻页缺少有效时间游标')
            params.update({'id': 1000, 'last_time': cursor, 'rn': 20})
        query = urlencode(sorted(params.items()))
        # 公开网页客户端的请求签名，无密钥；变化时让请求显式失败。
        sha1 = hashlib.sha1(query.encode()).hexdigest()
        sign = hashlib.md5(sha1.encode()).hexdigest()
        return f'{CLS_HOME if page == 0 else CLS_PAGE}?{query}&sign={sign}', None
    if code == 'goldman_insights':
        return GOLDMAN_FEED, None
    if code == 'citi_insights':
        return CITI_LIST, {'applyBusinessTypeBlacklist': False, 'applyTopicBlacklist': True,
            'getLatestForBusinessType': False, 'orderBy': ['content_json.publishDate', 'modified_gmt'],
            'pageIndex': page, 'pageSize': '9', 'hideSeries': True}
    if code == 'jpmorgan_research':
        return JPM_LIST.format(page=page + 1), None
    raise ValueError(SOURCES[code].unavailable_reason)


def parse_list(code, body, page):
    data = json.loads(body)
    if code == 'cls_depth':
        raw = _cls_rows(data, page)
        more = bool(raw)
        entries = [{'url': f"https://www.cls.cn/detail/{row['id']}", 'id': row['id'],
                    'title': row['title'], 'summary': row.get('brief') or '',
                    'published_at': datetime.fromtimestamp(row['ctime'], UTC), 'section': '头条'}
                   for row in raw if not row.get('is_ad') and not row.get('external_link')
                   and row.get('type') != -1]
    elif code == 'goldman_insights':
        if not isinstance(data, list):
            raise ValueError('Goldman Sachs 公开文章索引结构无效')
        articles = []
        for row in data:
            if not isinstance(row, dict) or not str(row.get('path', '')).startswith('/insights/articles/'):
                continue
            if row.get('templateName') != 'rich-article-template':
                continue
            props = row.get('cmsPageProps')
            if not isinstance(props, dict):
                raise ValueError('Goldman Sachs 文章索引缺少类型信息')
            if props.get('pageType') != 'article':
                continue
            articles.append({'url': urljoin('https://www.goldmansachs.com', row['path']),
                             'id': props['id'], 'title': row['title'], 'summary': row.get('description') or '',
                             'published_at': published_time(props.get('publishDate')), 'section': 'Articles'})
        if not articles:
            raise ValueError('Goldman Sachs 公开文章索引为空')
        articles.sort(key=lambda row: row['published_at'] or datetime.max.replace(tzinfo=UTC), reverse=True)
        raw = articles[page * GOLDMAN_PAGE_SIZE:(page + 1) * GOLDMAN_PAGE_SIZE]
        more = (page + 1) * GOLDMAN_PAGE_SIZE < len(articles)
        entries = raw
    elif code == 'citi_insights':
        data = data['data']
        if data['number'] != page or not isinstance(data['last'], bool):
            raise ValueError('Citi 分页响应与请求不一致')
        raw = data['content']
        more = not data['last']
        entries = [{'url': row['url'], 'id': row['id'], 'title': row['title'], 'summary': row.get('summary') or '',
                    'published_at': published_time(row.get('publishGmt')), 'section': row.get('topic'),
                    'media_only': row.get('insightType') in ('Podcast', 'Video'),
                    'navigation_only': row.get('insightType') == 'Series',
                    'attachments': json.loads(row.get('attachment') or '[]')} for row in raw]
    elif code == 'jpmorgan_research':
        meta = data['meta']
        if meta['page'] != page + 1 or meta['max-pages'] < meta['page']:
            raise ValueError('J.P. Morgan 分页响应与请求不一致')
        raw = data['items']
        more = meta['page'] < meta['max-pages']
        entries = [{'url': urljoin('https://www.jpmorgan.com', row['link']), 'title': row['title'],
                    'summary': row.get('description') or '', 'published_at': published_time(row.get('date')),
                    'section': row.get('eyebrow'), 'media_only': row.get('type') in ('video', 'podcast')}
                   for row in raw]
    else:
        raise ValueError(SOURCES[code].unavailable_reason)
    if code == 'cls_depth':
        if not raw and page == 0:
            raise ValueError('财联社头条首页为空，不能确认历史已结束')
        if raw and (not isinstance(raw[-1].get('ctime'), int) or raw[-1]['ctime'] <= 0):
            raise ValueError('财联社头条分页缺少有效时间游标')
    if not raw and (page == 0 or more):
        raise ValueError('文章列表为空或结构不完整，不能确认历史已结束')
    for row in entries:
        if not row['title'].strip() or not allowed_url(code, row['url']) or file_url(row['url']):
            raise ValueError('列表包含无效文章标题或站外／附件地址')
        row['url'] = canonical_url(row['url'])
    return entries, more


def goldman_article_content(selector, entry):
    raw = selector.css('script#__NEXT_DATA__::text').get()
    if not raw:
        raise ValueError('Goldman Sachs 文章缺少服务端详情数据')
    props = json.loads(raw)['props']['pageProps']
    cms = props['cmsPageProps']
    if cms['id'] != entry['id']:
        raise ValueError('Goldman Sachs 文章详情与列表 ID 不一致')
    if cms['pageType'] != 'article':
        return None
    sections = props['children']['children']
    header = next((i for i, section in enumerate(sections) if any(
        child.get('type') == 'article-header' for child in section.get('children', []))), None)
    if header is None:
        raise ValueError('Goldman Sachs 文章缺少正文边界')
    content = []
    for section in sections[header + 1:]:
        children = section.get('children', [])
        if any(child.get('type') in ('tags', 'tout-card-grid', 'footer') for child in children):
            break
        for child in children:
            if child.get('type') == 'rich-text':
                attributes = child['attributes']
                if 'text' not in attributes:
                    continue
                text = attributes['text']
                if not isinstance(text, str):
                    raise ValueError('Goldman Sachs 正文格式无效')
                content.append(text)
            elif child.get('type') == 'accordion':
                attributes = child['attributes']
                items = attributes.get('accordionContent')
                if not isinstance(items, list):
                    raise ValueError('Goldman Sachs 折叠正文格式无效')
                title = attributes.get('sectionTitle')
                if title:
                    content.append(f'<h2>{escape(title)}</h2>')
                for item in items:
                    question = item['visibleContent']
                    answer = item['hiddenContent']
                    if not isinstance(question, str) or not isinstance(answer, str):
                        raise ValueError('Goldman Sachs 折叠正文格式无效')
                    content.append(f'<h3>{escape(question)}</h3>{answer}')
    return '\n'.join(content)


def article_schema(selector):
    def walk(value):
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            if value.get('@type') in ('Article', 'NewsArticle', 'BlogPosting'):
                yield value
            yield from walk(value.get('@graph', []))
    for raw in selector.css('script[type="application/ld+json"]::text').getall():
        try:
            yield from walk(json.loads(raw))
        except (ValueError, TypeError):
            continue


def parse_article(code, url, body, entry, status=200):
    selector = Selector(body)
    if code == 'jpmorgan_research' and status == 200 and selector.css('body.topic-page'):
        # 列表的 default 类型也包含文章；仅跳过详情明确声明的专题导航页。
        return None
    if code == 'cls_depth' and status == 200:
        raw_detail = selector.css('script#__NEXT_DATA__::text').get()
        if not raw_detail:
            raise ValueError('财联社文章缺少服务端详情数据')
        detail = json.loads(raw_detail)['props']['pageProps']['articleDetail']
        if detail['id'] != entry.get('id'):
            raise ValueError('财联社文章详情与列表 ID 不一致')
        if detail['column']['columnName'] != '文章':
            return None
    else:
        detail = None
    goldman_content = goldman_article_content(selector, entry) if code == 'goldman_insights' and status == 200 else None
    schema = next(article_schema(selector), {})
    canonical = urljoin(url, selector.css('link[rel="canonical"]::attr(href)').get() or url)
    if not allowed_url(code, canonical):
        raise ValueError('文章 canonical 指向未允许的站点')
    canonical = canonical_url(canonical)
    # 标题和摘要优先用真实列表元数据；受限详情不能用拒绝页覆盖它们。
    title = entry.get('title') or schema.get('headline') or selector.css('h1').xpath('string(.)').get()
    if not title:
        raise ValueError('缺少文章标题')
    summary = entry.get('summary') or selector.css('meta[name="description"]::attr(content)').get() or ''
    bylines = []
    authors = schema.get('author', [])
    if not isinstance(authors, list):
        authors = [authors]
    for author in authors:
        author = {'name': author} if isinstance(author, str) else author
        if author.get('name'):
            bylines.append({'name': author['name'], 'profile_url': author.get('url'),
                            'organization': author['name'] if author.get('@type') == 'Organization' else None})
    meta_author = selector.css('meta[name="author"]::attr(content)').get()
    if not bylines and meta_author:
        bylines = [{'name': meta_author, 'profile_url': None, 'organization': None}]
    if not bylines and detail and isinstance(detail.get('author'), dict) and detail['author'].get('name'):
        bylines = [{'name': detail['author']['name'], 'profile_url': None, 'organization': None}]
    is_excerpt = False
    if code == 'cls_depth':
        content = detail.get('content', '') if detail else ''
        nodes = []
    elif code == 'goldman_insights':
        if status == 200 and goldman_content is None:
            return None
        content = goldman_content or ''
        nodes = []
    elif code == 'citi_insights':
        nodes = selector.css('.bodyContent')
        if not nodes:
            # 公开报告落地页可能只有简介和 PDF 链接；明确保存节选，不冒充全文。
            intro = selector.css('#InsightDetail .insight-detail-templates-common-style '
                                 '> .citigroup-container-sm-center > [class^="summary___"]')
            if any(file_url(urljoin(url, a.attrib['href'])) for a in intro.css('a[href]')):
                nodes = intro
                is_excerpt = True
    elif code == 'jpmorgan_research':
        if selector.css('body.raw-html-page'):
            # 旧版长文正文是独立 section；排除目录、相关推荐和弹窗副本。
            nodes = selector.css('body.raw-html-page .raw-html-body > section:not(.section-hero)'
                                 ':not(#table-of-contents):not(#related-insights-header):not(#cutting-edge)')
        elif selector.css('body.immersive-page'):
            nodes = selector.css('body.immersive-page .immersive-text-block-componentMainContainer .cmp-text, '
                                 'body.immersive-page .immersive-page-header-title, '
                                 'body.immersive-page .FullWidthImmersiveImageMainContainer .imageDiv, '
                                 'body.immersive-page .cmp-infographic .infographic-title, '
                                 'body.immersive-page .cmp-infographic .infographic-thumbnail-img, '
                                 'body.immersive-page .infographic-txt')
        else:
            nodes = selector.css('body.article-page .cmp-text, body.article-page .cmp-infographic .infographic-title, '
                                 'body.article-page .cmp-infographic .infographic-thumbnail-img, '
                                 'body.article-page .infographic-txt')
    else:
        # 未验证来源不猜正文节点，也不能将全站文字当作全文。
        nodes = selector.css('[data-research-body-not-verified]')
    content = content if code in ('cls_depth', 'goldman_insights') and status == 200 else '\n'.join(nodes.getall()) if status == 200 else ''
    if not isinstance(content, str):
        raise ValueError('文章正文格式无效')
    paywall = (detail.get('isFree') is False if detail else False) or schema.get('isAccessibleForFree') in (False, 'false')
    access = 'forbidden' if status in (401, 403, 429) else 'paywall' if paywall else 'public' if status == 200 else 'unknown'
    has_body = bool(html_to_text(content)) or (code == 'cls_depth' and any(
        image.attrib.get('src') for image in Selector(content).css('img[src]')))
    content_status = ('excerpt' if paywall or is_excerpt else 'full') if has_body else (
        ('excerpt' if summary else 'metadata') if access in ('forbidden', 'paywall') else 'extraction_failed')
    # 纯音视频介绍不入库；只有列表声明媒体且未提供网页文章正文时才排除。
    if not has_body and (entry.get('media_only') or (status == 200 and entry.get('navigation_only'))):
        return None
    attachments = []
    candidates = [(a.get('link'), a.get('name')) for a in entry.get('attachments', [])]
    candidates += [(a.attrib.get('href'), a.xpath('string(.)').get()) for a in selector.css('a[href]')
                   if file_url(urljoin(url, a.attrib['href']))]
    for href, label in candidates:
        link = urljoin(url, (href or '').strip())
        if urlsplit(link).scheme in ('https', 'http') and file_url(link) and link not in [a['url'] for a in attachments]:
            attachments.append({'url': link, 'title': label or None, 'status': 'link_only'})
    date_value = schema.get('datePublished') or selector.css('meta[name="publishDate"]::attr(content)').get()
    if code == 'goldman_insights' and date_value == 'No published date provided':
        date_value = None
    date = (datetime.fromtimestamp(detail['ctime'], UTC) if detail and detail.get('ctime') else None) or entry.get('published_at') or published_time(date_value)
    metadata = {'publisher_code': code, 'publisher_name': SOURCES[code].name, 'bylines': bylines,
                'canonical_url': canonical, 'original_url': None,
                'language': selector.css('html::attr(lang)').get(), 'section': entry.get('section'),
                'content_status': content_status, 'access_status': access, 'attachments': attachments}
    return {'external_id': identifier(canonical, entry.get('id')), 'url': canonical, 'title': title.strip(),
            'summary': summary, 'content_html': content, 'author': '; '.join(a['name'] for a in bylines) or None,
            'published_at': date or datetime.now(UTC), 'is_paywalled': paywall,
            'extra': {'research': metadata, 'published_at_precision': 'date' if date else 'unknown',
                      'published_date': date.date().isoformat() if date else None,
                      'published_at_unreliable': date is None}}
