import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from parsel import Selector
from scrapy.http import HtmlResponse

from alphapulse.core.research_sources import SOURCES
@pytest.fixture(autouse=True)
def modules(app_env):
    global parser, RunState, CitiInsightsSpider, ClsDepthSpider, JpmorganResearchSpider, GoldmanInsightsSpider
    from alphapulse.crawler import research_articles as parser
    from alphapulse.crawler.spiders.base import RunState
    from alphapulse.crawler.spiders.research_articles import (CitiInsightsSpider, ClsDepthSpider, GoldmanInsightsSpider,
                                                               JpmorganResearchSpider)

FIXTURES = Path(__file__).parent / 'fixtures' / 'research'


def cls_list(rows, *, first=False, top=None):
    return json.dumps({'errno': 0, 'data': {'depth_list': rows, 'top_article': top or []} if first else rows})


def test_cls_public_headline_sign_and_pagination():
    assert SOURCES['cls_depth'].availability == 'ready'
    home, payload = parser.list_request('cls_depth', 0)
    assert home == ('https://www.cls.cn/v3/depth/home/assembled/1000?'
                    'app=CailianpressWeb&os=web&sv=8.7.9&sign=b02d8f7bc4c45eeb3e86904203597da2')
    assert payload is None
    with pytest.raises(ValueError, match='游标'):
        parser.list_request('cls_depth', 1)
    url, payload = parser.list_request('cls_depth', 1, 1794625773)
    assert url == ('https://www.cls.cn/v3/depth/list/1000?app=CailianpressWeb&id=1000&'
                   'last_time=1794625773&os=web&rn=20&sv=8.7.9&sign=8999dc6e2019495a3481e09c81ca9f32')
    assert payload is None
    first = cls_list([{'id': 2534588, 'ctime': 1794692346, 'title': '见证历史+1', 'brief': '正文摘要',
                       'is_ad': 0},
                      {'id': 2534408, 'ctime': 1794625773, 'title': '全球化、电动化、数字化', 'is_ad': 0}],
                     first=True, top=[{'id': 2534623, 'type': -1, 'title': '电报'}])
    entries, more = parser.parse_list('cls_depth', first, 0)
    assert more and [e['id'] for e in entries] == [2534588, 2534408]
    assert entries[0]['url'] == 'https://www.cls.cn/detail/2534588'
    assert entries[0]['published_at'] == datetime.fromtimestamp(1794692346, UTC)
    assert parser.cls_cursor(first, 0) == 1794625773
    second = cls_list([{'id': 2534388, 'ctime': 1794620959, 'title': '圆桌共识'},
                       {'id': 2534300, 'ctime': 1794600000, 'title': '广告', 'is_ad': 1},
                       {'id': 2534299, 'ctime': 1794599999, 'title': '站外', 'external_link': 'https://other.test'},
                       {'id': 2534298, 'ctime': 1794599998, 'title': '电报', 'type': -1}])
    entries, more = parser.parse_list('cls_depth', second, 1)
    assert more and [e['id'] for e in entries] == [2534388]
    assert parser.cls_cursor(second, 1) == 1794599998
    assert parser.parse_list('cls_depth', cls_list([]), 2) == ([], False)
    with pytest.raises(ValueError, match='请求失败'):
        parser.parse_list('cls_depth', json.dumps({'errno': 10012, 'msg': '签名错误'}), 0)
    with pytest.raises(ValueError, match='首页为空'):
        parser.parse_list('cls_depth', cls_list([], first=True), 0)


def test_cls_article_is_full_and_telegraph_is_excluded():
    url = 'https://www.cls.cn/detail/2534588'
    entry = {'id': 2534588, 'title': '见证历史+1', 'summary': '正文摘要',
             'published_at': datetime.fromtimestamp(1794692346, UTC), 'section': '头条'}
    detail = {'id': 2534588, 'column': {'columnName': '文章'}, 'ctime': 1794692346,
              'isFree': True, 'author': {'name': '财联社 史正丞'},
              'content': '<p>财联社11月15日讯，飞船返回得克萨斯州星港。</p>'}
    body = (f'<html lang="zh"><script id="__NEXT_DATA__" type="application/json">'
            f'{json.dumps({"props": {"pageProps": {"articleDetail": detail}}}, ensure_ascii=False)}'
            '</script><div class="detail-content">正文</div></html>')
    item = parser.parse_article('cls_depth', url, body, entry)
    assert item['external_id'] == '2534588'
    assert item['extra']['research']['content_status'] == 'full'
    assert item['extra']['research']['section'] == '头条'
    assert item['author'] == '财联社 史正丞'
    assert item['published_at'] == entry['published_at']
    assert '飞船返回' in item['content_html'] and '正文</div>' not in item['content_html']
    telegraph = body.replace('"文章"', '"电报"')
    assert parser.parse_article('cls_depth', url, telegraph, entry) is None
    with pytest.raises(ValueError, match='ID 不一致'):
        parser.parse_article('cls_depth', url, body.replace('"id": 2534588', '"id": 2534623'), entry)
    with pytest.raises(ValueError, match='服务端详情数据'):
        parser.parse_article('cls_depth', url, '<html><nav>空框架</nav></html>', entry)
    denied = parser.parse_article('cls_depth', url, '<h1>403</h1>', entry, 403)
    assert denied['extra']['research']['access_status'] == 'forbidden'
    assert denied['extra']['research']['content_status'] == 'excerpt'


def test_cls_image_only_article_has_body():
    url = 'https://www.cls.cn/detail/2533940'
    entry = {'id': 2533940, 'title': '一图看懂', 'summary': '一张公开图解',
             'published_at': datetime.fromtimestamp(1794561191, UTC), 'section': '头条'}
    detail = {'id': 2533940, 'column': {'columnName': '文章'}, 'ctime': 1794561191,
              'isFree': True, 'content': '<p><img src="https://image.cls.cn/images/20261113/figure.jpg"></p>'}
    def body():
        return ('<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({'props': {'pageProps': {'articleDetail': detail}}}) + '</script></html>')

    item = parser.parse_article('cls_depth', url, body(), entry)
    assert item['extra']['research']['content_status'] == 'full'
    assert item['content_html'] == detail['content']
    detail['content'] = '<p><img src=""></p>'
    assert parser.parse_article('cls_depth', url, body(), entry)['extra']['research']['content_status'] == 'extraction_failed'
    detail['content'] = ''
    assert parser.parse_article('cls_depth', url, body(), entry)['extra']['research']['content_status'] == 'extraction_failed'


def test_cls_history_cursor_and_cutoff():
    async def check():
        spider = ClsDepthSpider()
        now = datetime(2026, 11, 16, tzinfo=UTC)
        spider.run = RunState(uuid.uuid4(), 'ci', now, now-timedelta(days=1),
            history_target=now-timedelta(days=30), history_position=now,
            history_deadline=time.monotonic()+20)
        assert len(list(spider.first_requests())) == 1
        first = cls_list([{'id': 2534588, 'ctime': 1794692346, 'title': '头条文章'},
                          {'id': 2534408, 'ctime': 1794625773, 'title': '较早文章'}], first=True)
        request = spider.list_request(0, history=True, pages=1)
        result = [item async for item in spider.parse_list(HtmlResponse(request.url, request=request,
            body=first.encode(), encoding='utf-8'))]
        assert len(result) == 3
        next_page = result[-1]
        assert 'last_time=1794625773' in next_page.url
        assert spider.run.official_cursor['history'] == {'page': 1, 'last_time': 1794625773,
            'target': spider.run.history_target.isoformat()}
        second = cls_list([{'id': 2534388, 'ctime': 1794620959, 'title': '下一页文章'}])
        result = [item async for item in spider.parse_list(HtmlResponse(next_page.url, request=next_page,
            body=second.encode(), encoding='utf-8'))]
        assert len(result) == 2 and spider.run.official_cursor['history']['last_time'] == 1794620959
        assert spider.run.history_position == datetime.fromtimestamp(1794620959, UTC)
        next_page = result[-1]
        repeated = cls_list([{'id': 2534388, 'ctime': 1794620959, 'title': '下一页文章'}])
        with pytest.raises(ValueError, match='未前进'):
            [item async for item in spider.parse_list(HtmlResponse(next_page.url, request=next_page,
                body=repeated.encode(), encoding='utf-8'))]
        spider.run.history_target = now
        assert [item async for item in spider.parse_list(HtmlResponse(request.url, request=request,
            body=first.encode(), encoding='utf-8'))] == []
        assert spider.run.history_done
    asyncio.run(check())


def test_cls_history_resumes_saved_cursor(monkeypatch):
    from alphapulse.crawler.spiders import research_articles as spider_module
    now = datetime(2026, 11, 16, tzinfo=UTC)
    target = now - timedelta(days=30)
    saved = {'history': {'target': target.isoformat(), 'page': 5, 'last_time': 1794000000}}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def scalar(self, _):
            return saved

    monkeypatch.setattr(spider_module, 'SessionLocal', Session)
    spider = ClsDepthSpider()
    spider.run = RunState(uuid.uuid4(), 'ci', now, now-timedelta(days=1), history_target=target,
                          history_position=now, history_deadline=time.monotonic()+20)

    async def first_request():
        return [request async for request in spider.start_history(None)][0]

    request = asyncio.run(first_request())
    assert request.meta['page'] == 5 and request.meta['cursor'] == 1794000000
    assert 'last_time=1794000000' in request.url
    saved['history'].pop('last_time')
    request = asyncio.run(first_request())
    assert request.meta['page'] == 0 and request.meta['cursor'] is None
    assert '/home/assembled/1000?' in request.url


def goldman_feed(count=25):
    return json.dumps([
        {'path': f'/insights/articles/article-{day}', 'title': f'Article {day}',
         'created': 'Fri Nov 13 2026 18:40:42 GMT+0000', 'templateName': 'rich-article-template',
         'cmsPageProps': {'id': f'article-{day}', 'pageType': 'article',
                          'publishDate': f'2025-01-{day:02d}T00:00:00'}}
        for day in range(1, count + 1)
    ] + [{'path': '/insights/articles/video', 'title': 'Video', 'templateName': 'rich-article-template',
          'cmsPageProps': {'id': 'video', 'pageType': 'video', 'publishDate': '2026-11-14T00:00:00'}},
         {'path': '/insights/articles/landing', 'title': 'Landing', 'templateName': 'landing-page-template'},
         {'path': '/insights/podcasts/episode', 'title': 'Podcast', 'templateName': 'rich-article-template'}])


def test_goldman_feed_pagination_uses_original_publication_date():
    assert SOURCES['goldman_insights'].availability == 'ready'
    assert parser.list_request('goldman_insights', 0) == ('https://www.goldmansachs.com/feeds/insights.json', None)
    feed = goldman_feed()
    first, more = parser.parse_list('goldman_insights', feed, 0)
    second, last = parser.parse_list('goldman_insights', feed, 1)
    assert more and not last and len(first) == 20 and len(second) == 5
    assert first[0]['url'] == 'https://www.goldmansachs.com/insights/articles/article-25'
    assert first[0]['published_at'] == datetime(2025, 1, 25, tzinfo=UTC)
    assert second[-1]['id'] == 'article-1'
    assert parser.parse_list('goldman_insights', feed, 2) == ([], False)
    with pytest.raises(ValueError, match='索引结构无效'):
        parser.parse_list('goldman_insights', '{}', 0)
    with pytest.raises(ValueError, match='索引为空'):
        parser.parse_list('goldman_insights', '[]', 0)


def test_goldman_unknown_publication_date_is_not_skipped_or_invented():
    feed = json.loads(goldman_feed())
    feed.append({'path': '/insights/articles/undated', 'title': 'Undated',
                 'created': 'Mon Jul 22 2024 18:33:09 GMT+0000', 'templateName': 'rich-article-template',
                 'cmsPageProps': {'id': 'undated', 'pageType': 'article', 'publishDate': None}})
    first, more = parser.parse_list('goldman_insights', json.dumps(feed), 0)
    assert more and first[0]['id'] == 'undated' and first[0]['published_at'] is None
    data = {'props': {'pageProps': {'cmsPageProps': {'id': 'undated', 'pageType': 'article'},
            'children': {'children': [{'children': [{'type': 'article-header'}]},
                                      {'children': [{'type': 'rich-text', 'attributes': {'text': '<p>Body.</p>'}}]}]}}}}
    body = ('<script type="application/ld+json">{"@type":"Article","datePublished":"No published date provided"}</script>'
            '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>')
    item = parser.parse_article('goldman_insights', first[0]['url'], body, first[0])
    assert item['extra']['research']['content_status'] == 'full'
    assert item['extra']['published_at_unreliable'] is True
    assert item['extra']['published_date'] is None


def test_goldman_article_detail_is_article_body_only():
    url = 'https://www.goldmansachs.com/insights/articles/article-25'
    entry = {'id': 'article-25', 'url': url, 'title': 'Article 25', 'summary': 'Public summary',
             'published_at': datetime(2025, 1, 25, tzinfo=UTC), 'section': 'Articles'}
    page = {'props': {'pageProps': {
        'cmsPageProps': {'id': entry['id'], 'pageType': 'article', 'publishDate': '2025-01-25T00:00:00'},
        'children': {'children': [
            {'type': 'dptk-grid', 'children': [{'type': 'rich-text', 'attributes': {'text': '<p>Navigation</p>'}}]},
            {'type': 'dptk-grid', 'children': [{'type': 'article-header', 'attributes': {'title': 'Article 25'}}]},
            {'type': 'dptk-grid', 'children': [{'type': 'article-summary'},
                {'type': 'rich-text', 'attributes': {'text': '<p>First paragraph.</p>'}},
                {'type': 'rich-text', 'attributes': {'text': '<p>Second paragraph.</p>'}}]},
            {'type': 'dptk-grid', 'children': [{'type': 'tags'},
                {'type': 'rich-text', 'attributes': {'text': '<p>Related content</p>'}}]},
        ]}}}}
    def body():
        return '<html lang="en"><script id="__NEXT_DATA__">' + json.dumps(page) + '</script></html>'

    item = parser.parse_article('goldman_insights', url, body(), entry)
    assert item['external_id'] == 'article-25'
    assert item['extra']['research']['content_status'] == 'full'
    assert item['published_at'] == entry['published_at']
    assert item['content_html'] == '<p>First paragraph.</p>\n<p>Second paragraph.</p>'
    assert 'Navigation' not in item['content_html'] and 'Related content' not in item['content_html']
    page['props']['pageProps']['cmsPageProps']['id'] = 'another-article'
    with pytest.raises(ValueError, match='ID 不一致'):
        parser.parse_article('goldman_insights', url, body(), entry)
    page['props']['pageProps']['cmsPageProps'].update(id=entry['id'], pageType='video')
    assert parser.parse_article('goldman_insights', url, body(), entry) is None
    page['props']['pageProps']['cmsPageProps']['pageType'] = 'article'
    page['props']['pageProps']['children']['children'][2]['children'] = []
    assert parser.parse_article('goldman_insights', url, body(), entry)['extra']['research']['content_status'] == 'extraction_failed'


def test_goldman_accordion_and_empty_rich_text_keep_article_body():
    url = 'https://www.goldmansachs.com/insights/articles/how-automation-impacts-the-lives-and-careers-of-displaced-workers'
    entry = {'id': '3095a95e-2935-4657-ac84-5c0f411f4a44', 'url': url, 'title': 'Automation'}
    page = {'props': {'pageProps': {
        'cmsPageProps': {'id': entry['id'], 'pageType': 'article'},
        'children': {'children': [
            {'children': [{'type': 'article-header'}]},
            {'children': [
                {'type': 'rich-text', 'attributes': {'text': '<p>First paragraph.</p>'}},
                {'type': 'accordion', 'attributes': {'sectionTitle': 'FAQ', 'accordionContent': [
                    {'visibleContent': 'What happens next?', 'hiddenContent': '<p>Workers retrain.</p>'}]}},
                {'type': 'rich-text', 'attributes': {'bottomPadding': 'noBottomPadding'}},
                {'type': 'rich-text', 'attributes': {'text': '<p>Last paragraph.</p>'}}]},
            {'children': [{'type': 'tags'}]},
        ]}}}}
    body = '<script id="__NEXT_DATA__">' + json.dumps(page) + '</script>'
    item = parser.parse_article('goldman_insights', url, body, entry)
    assert item['extra']['research']['content_status'] == 'full'
    assert item['content_html'] == ('<p>First paragraph.</p>\n<h2>FAQ</h2>\n'
                                    '<h3>What happens next?</h3><p>Workers retrain.</p>\n'
                                    '<p>Last paragraph.</p>')


def test_goldman_history_resumes_pages_and_realtime_stays_on_first_page(monkeypatch):
    from alphapulse.crawler.spiders import research_articles as spider_module
    now = datetime(2025, 2, 1, tzinfo=UTC)
    target = datetime(2025, 1, 1, tzinfo=UTC)
    spider = GoldmanInsightsSpider()
    spider.run = RunState(uuid.uuid4(), 'ci', now, target, history_target=target,
                          history_position=now, history_deadline=time.monotonic() + 20)
    feed = goldman_feed()

    async def parse(request):
        response = HtmlResponse(request.url, request=request, body=feed.encode(), encoding='utf-8')
        return [result async for result in spider.parse_list(response)]

    realtime = asyncio.run(parse(next(iter(spider.first_requests()))))
    assert len(realtime) == 20
    history = asyncio.run(parse(spider.list_request(0, history=True, pages=1)))
    assert len(history) == 21 and history[-1].meta['page'] == 1
    assert spider.run.official_cursor['history'] == {'page': 1, 'target': target.isoformat()}
    assert not spider.run.history_done
    last = asyncio.run(parse(history[-1]))
    assert len(last) == 5 and spider.run.history_done
    assert spider.run.history_position == target

    saved = {'history': {'target': target.isoformat(), 'page': 1}}
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def scalar(self, _):
            return saved
    monkeypatch.setattr(spider_module, 'SessionLocal', Session)
    request = asyncio.run(anext(spider.start_history(None)))
    assert request.meta['page'] == 1 and request.url == parser.GOLDMAN_FEED


def test_public_lists_have_distinct_next_page_and_explicit_last_page():
    first, more = parser.parse_list('citi_insights', (FIXTURES / 'citi-xhr-1.json').read_text(), 0)
    second, _ = parser.parse_list('citi_insights', (FIXTURES / 'citi-page2.json').read_text(), 1)
    assert more and first and second and {e['url'] for e in first}.isdisjoint(e['url'] for e in second)
    items, more = parser.parse_list('jpmorgan_research', (FIXTURES / 'jpmorgan-xhr-1.json').read_text(), 0)
    assert len(items) == 93 and not more


@pytest.mark.parametrize('code,file,url', [
    ('citi_insights', 'citi-detail.html', 'https://www.citigroup.com/global/insights/tokenization-2030'),
    ('citi_insights', 'citi-detail2.html', 'https://www.citigroup.com/global/insights/pix-commercial-adoption-instant-payments'),
    ('jpmorgan_research', 'jpmorgan-detail.html', 'https://www.jpmorgan.com/insights/global-research/real-estate/housing-affordability'),
])
def test_real_article_fragments(code, file, url):
    item = parser.parse_article(code, url, (FIXTURES / file).read_text(), {'url': url, 'title': '公开文章'})
    assert item['extra']['research']['content_status'] == 'full'
    assert item['content_html'] and '<script' not in item['content_html']
    if code == 'jpmorgan_research':
        assert item['author'] == 'J.P. Morgan'
        assert item['published_at'].date().isoformat() == '2026-10-12'
        from alphapulse.crawler.content_policy import clean_html
        content = Selector(clean_html(item['content_html'], item['url']))
        prefix = 'https://www.jpmorgan.com/content/dam/jpmorgan/images/cib/global-research/housing_affordability_2026/'
        assert content.css('img::attr(src)').getall() == [prefix+'house_prices_graph.svg', prefix+'housing-graph.svg']
        assert content.xpath('string(.)').get().count('Real home prices in the U.S. and OECD countries') == 1
        assert not content.css('button, .modal')


@pytest.mark.parametrize('code', [code for code, spec in SOURCES.items() if spec.availability != 'ready'])
def test_unverified_sources_cannot_claim_success(code):
    with pytest.raises(ValueError, match=SOURCES[code].unavailable_reason):
        parser.list_request(code, 0)


def test_empty_framework_and_access_denial_are_not_full_articles():
    url = 'https://www.citigroup.com/global/insights/one'
    entry = {'url': url, 'title': '真实列表标题', 'summary': '真实摘要'}
    failed = parser.parse_article('citi_insights', url, '<html><nav>拒绝页</nav></html>', entry)
    assert failed['extra']['research']['content_status'] == 'extraction_failed'
    assert failed['extra']['published_at_unreliable']
    denied = parser.parse_article('citi_insights', url, '<h1>Access denied</h1>', entry, 403)
    assert denied['title'] == entry['title'] and denied['extra']['research']['content_status'] == 'excerpt'
    assert not denied['content_html'] and denied['extra']['research']['access_status'] == 'forbidden'
    entry['summary'] = ''
    assert parser.parse_article('citi_insights', url, '', entry, 403)['extra']['research']['content_status'] == 'metadata'
    entry['media_only'] = True
    assert parser.parse_article('citi_insights', url, '', entry) is None
    with pytest.raises(ValueError):
        parser.parse_list('citi_insights', json.dumps({'data': {'number': 0, 'last': True, 'content': []}}), 0)


def test_citi_series_navigation_does_not_block_history_or_hide_articles():
    row = {'url': 'https://www.citigroup.com/global/insights/series', 'id': 'series',
           'title': 'Podcast series', 'insightType': 'Series'}
    entries, more = parser.parse_list('citi_insights', json.dumps({'data': {
        'number': 0, 'last': True, 'content': [row]}}), 0)
    assert not more and entries[0]['navigation_only']
    assert parser.parse_article('citi_insights', row['url'], '<nav>Episodes</nav>', entries[0]) is None
    article = parser.parse_article('citi_insights', row['url'], '<div class="bodyContent">Written article</div>', entries[0])
    assert article['extra']['research']['content_status'] == 'full'
    denied = parser.parse_article('citi_insights', row['url'], 'Forbidden', entries[0], 403)
    assert denied['extra']['research']['access_status'] == 'forbidden'


def test_citi_report_landing_page_is_excerpt_with_attachment_not_full():
    url = 'https://www.citigroup.com/global/insights/guidance-to-combat-fraud'
    body = (FIXTURES / 'citi-report-intro.html').read_text()
    entry = {'url': url, 'title': 'Guidance on How to Combat Fraud'}
    item = parser.parse_article('citi_insights', url, body, entry)
    meta = item['extra']['research']
    assert meta['content_status'] == 'excerpt' and meta['access_status'] == 'public'
    assert 'Cyber criminals' in item['content_html'] and 'Companies worldwide' not in item['content_html']
    assert len(meta['attachments']) == 1 and meta['attachments'][0]['status'] == 'link_only'
    assert meta['attachments'][0]['url'].endswith('2344153_Guidance_HowToCombatFraud.pdf')
    # 没有正文且无附件证据仍明确失败；真实正文优先于简介。
    no_report = parser.parse_article('citi_insights', url, body.replace('.pdf', '/overview'), entry)
    assert no_report['extra']['research']['content_status'] == 'extraction_failed'
    article = parser.parse_article('citi_insights', url, body.replace('</body>', '<div class="bodyContent">Full article</div></body>'), entry)
    assert article['extra']['research']['content_status'] == 'full'
    assert 'Cyber criminals' not in article['content_html']


def test_canonical_identity_attachment_and_own_byline():
    url = 'https://www.citigroup.com/global/insights/one?series=2&utm_source=x#ref'
    assert parser.identifier(url) == parser.identifier(url.replace('utm_source=x#ref', 'utm_source=y'))
    assert parser.identifier(url) != parser.identifier(url.replace('series=2', 'series=3'))
    html = '<div class="bodyContent"><p>正文</p><a href="/report.pdf">报告</a></div><script type="application/ld+json">{"@type":"Article","author":[{"name":"作者一"}],"isAccessibleForFree":false}</script>'
    item = parser.parse_article('citi_insights', url, html, {'url': url, 'title': '文章'})
    assert item['author'] == '作者一' and item['extra']['research']['content_status'] == 'excerpt'
    assert item['extra']['research']['attachments'] == [{'url': 'https://www.citigroup.com/report.pdf', 'title': '报告', 'status': 'link_only'}]
    with pytest.raises(ValueError, match='canonical'):
        parser.parse_article('citi_insights', url, '<link rel="canonical" href="https://evil.invalid/a">', {'url': url, 'title': '文章'})


@pytest.mark.parametrize('file,phrase', [
    ('jpmorgan-raw-template.html', 'The U.S. dollar has been under pressure'),
    ('jpmorgan-immersive-template.html', 'Yet its share of global spending'),
])
def test_historical_article_templates_exclude_navigation_and_keep_images(file, phrase):
    from alphapulse.crawler.content_policy import clean_html
    url = 'https://www.jpmorgan.com/insights/global-research/history'
    item = parser.parse_article('jpmorgan_research', url, (FIXTURES/file).read_text(), {'title': 'Research'})
    assert item['extra']['research']['content_status'] == 'full'
    content = Selector(clean_html(item['content_html'], url))
    assert phrase in content.xpath('string(.)').get()
    assert len(content.css('img')) == 1
    assert all(term not in content.get() for term in ('GLOBAL NAVIGATION', 'TABLE OF CONTENTS', 'RELATED', 'FOOTER', 'DUPLICATE'))


@pytest.mark.parametrize('location,redirects,allowed', [
    ('/insights/global-research/commodities/oil-prices', 0, True),
    ('https://www.jpmorgan.com/insights/global-research/commodities/oil-prices', 2, True),
    ('https://other.invalid/article', 0, False),
    ('https://www.jpmorgan.com/report.pdf', 0, False),
    ('/insights/global-research/commodities/oil-prices', 3, False),
    ('', 0, False),
])
def test_article_redirects_are_bounded_and_stay_on_verified_site(location, redirects, allowed):
    async def check():
        spider = JpmorganResearchSpider()
        now = datetime(2026, 11, 14, tzinfo=UTC)
        spider.run = RunState(uuid.uuid4(), 'ci', now, now-timedelta(days=30))
        entry = {'title': 'Oil prices'}
        request = spider.request('https://www.jpmorgan.com/insights/global-research/commodities/oil-price-forecast',
                                 spider.parse_article, entry=entry, cutoff=now, research_redirects=redirects)
        response = HtmlResponse(request.url, request=request, status=301, headers={'Location': location})
        if not allowed:
            with pytest.raises(ValueError, match='重定向'):
                [item async for item in spider.parse_article(response)]
        else:
            result = [item async for item in spider.parse_article(response)]
            assert len(result) == 1 and result[0].url.endswith('/oil-prices')
            assert result[0].meta['entry'] == entry and result[0].meta['cutoff'] == now
            assert result[0].meta['research_redirects'] == redirects + 1
    asyncio.run(check())


def test_page_budget_cutoff_and_history_checkpoint():
    asyncio.run(page_budget_cutoff_and_history_checkpoint())


async def page_budget_cutoff_and_history_checkpoint():
    spider = CitiInsightsSpider()
    now = datetime(2026, 11, 14, tzinfo=UTC)
    spider.run = RunState(uuid.uuid4(), 'ci', now, now - timedelta(days=30), history_target=now-timedelta(days=30), history_position=now, history_deadline=time.monotonic()+20)
    request = spider.list_request(0, history=True, pages=20)
    response = HtmlResponse(request.url, request=request, body=(FIXTURES/'citi-xhr-1.json').read_bytes(), encoding='utf-8')
    requests = [item async for item in spider.parse_list(response)]
    assert requests and all('entry' in item.meta for item in requests)
    assert spider.run.official_cursor['history']['page'] == 1 and not spider.run.history_done
    spider.run.history_target = now
    assert [item async for item in spider.parse_list(response)] == []
    assert spider.run.history_done
    from alphapulse.crawler.spiders.research_articles import BlackrockInsightsSpider
    blocked = BlackrockInsightsSpider()
    with pytest.raises(ValueError):
        list(blocked.first_requests())


@pytest.mark.parametrize('history', [False, True])
@pytest.mark.parametrize('detail_date', ['2023-07-06', '2026-10-15', '2026-10-20', '2026-11-01', None])
def test_detail_date_respects_each_lane_cutoff(history, detail_date):
    async def check():
        spider = JpmorganResearchSpider()
        now = datetime(2026, 11, 14, tzinfo=UTC)
        spider.run = RunState(uuid.uuid4(), 'ci', now, datetime(2026, 11, 1, tzinfo=UTC),
            history_target=datetime(2026, 10, 15, tzinfo=UTC), history_position=now,
            history_deadline=time.monotonic()+20)
        request = spider.list_request(0, history=history, pages=1)
        response = HtmlResponse(request.url, request=request,
            body=(FIXTURES/'jpmorgan-xhr-1.json').read_bytes(), encoding='utf-8')
        requests = [item async for item in spider.parse_list(response)]
        detail = next(item for item in requests if item.url.endswith('/gender-equity'))
        assert detail.meta['entry']['published_at'] is None
        cutoff = spider.run.history_target if history else spider.run.cutoff
        assert detail.meta['cutoff'] == cutoff
        date_meta = f'<meta name="publishDate" content="{detail_date}">' if detail_date else ''
        body = f'<html><head>{date_meta}</head><body class="article-page"><div class="cmp-text">正文</div></body></html>'
        detail.meta['raw_response_id'] = 1
        result = [item async for item in spider.parse_article(HtmlResponse(detail.url, request=detail,
            body=body.encode(), encoding='utf-8'))]
        expected = detail_date is None or datetime.fromisoformat(detail_date).replace(tzinfo=UTC) >= cutoff
        assert bool(result) == expected
        assert not spider.run.errors
    asyncio.run(check())


@pytest.mark.parametrize('page_class,body,expected_items,failed', [
    ('topic-page', '<div class="cmp-text">专题介绍与文章列表</div>', 0, False),
    ('article-page', '<div class="cmp-text">真实文章正文</div>', 1, False),
    ('article-page', '<nav>没有正文</nav>', 1, True),
])
def test_topic_page_is_skipped_without_hiding_article_failures(page_class, body, expected_items, failed):
    async def check():
        spider = JpmorganResearchSpider()
        now = datetime(2026, 11, 14, tzinfo=UTC)
        spider.run = RunState(uuid.uuid4(), 'ci', now, now-timedelta(days=30),
            history_target=now-timedelta(days=30), history_position=now, history_deadline=time.monotonic()+20)
        request = spider.list_request(0, history=True, pages=1)
        response = HtmlResponse(request.url, request=request,
            body=(FIXTURES/'jpmorgan-xhr-1.json').read_bytes(), encoding='utf-8')
        requests = [item async for item in spider.parse_list(response)]
        detail = next(item for item in requests if item.url.endswith('/sustainability'))
        detail.meta['raw_response_id'] = 1
        result = [item async for item in spider.parse_article(HtmlResponse(detail.url, request=detail,
            body=f'<body class="{page_class}">{body}</body>'.encode(), encoding='utf-8'))]
        assert len(result) == expected_items
        assert bool(spider.run.errors) == failed
        assert spider.run.history_done
    asyncio.run(check())

