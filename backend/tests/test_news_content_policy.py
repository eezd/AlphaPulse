"""新闻格式限制、内容稳定性及历史哈希兼容。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from scrapy import Request
from scrapy.exceptions import IgnoreRequest, StopDownload
from scrapy.http import Headers

from alphapulse.crawler.content_policy import clean_html, ignored_retry


@pytest.fixture
def guard(app_env):
    from alphapulse.crawler.middlewares import NewsContentPolicyMiddleware
    return NewsContentPolicyMiddleware()


def test_body_images_tables_and_real_changes_survive_normalization():
    html = '<p>政策正文\x00</p><script>noise</script><iframe src="a.pdf"></iframe><table><tr><td>3%</td></tr></table><img data-src="/chart.png" srcset="/a.png 1x, /b.png 2x"><figcaption>图表说明</figcaption>'
    value = clean_html(html, 'https://official.example/news/1')
    assert '政策正文' in value and '3%' in value and '图表说明' in value
    assert 'https://official.example/chart.png' in value
    assert 'https://official.example/a.png 1x' in value
    assert 'iframe' not in value and 'script' not in value and '\x00' not in value
    assert clean_html(value, 'https://official.example/news/1') == value
    assert clean_html(html.replace('3%', '4%')) != clean_html(html)


def test_email_share_encoding_is_not_news_content():
    html = '<p>Monetary policy.</p><a href="/cdn-cgi/l/email-protection#abc" data-cfemail="abc">Email</a>'
    assert clean_html(html) == clean_html(html.replace('abc', 'def'))


def test_picture_sources_and_lazy_candidates_resolve_to_publisher():
    html = ('<picture><source type="image/webp" srcset="/wide.webp 1x, ../large.webp 2x">'
        '<source media="(min-width: 800px)" data-srcset="//cdn.example/chart.webp 800w">'
        '<img src="/fallback.png" alt="政策图表"></picture>')
    base = 'https://official.example/news/article'
    value = clean_html(html, base)
    from parsel import Selector
    sel = Selector(value)
    assert sel.css('picture source::attr(srcset)').getall() == [
        'https://official.example/wide.webp 1x, https://official.example/large.webp 2x',
        'https://cdn.example/chart.webp 800w',
    ]
    assert sel.css('img::attr(src)').get() == 'https://official.example/fallback.png'
    assert sel.css('img::attr(alt)').get() == '政策图表'
    assert clean_html(value, base) == value


@pytest.mark.parametrize('url', ['https://official.example/a.PDF?download=1', 'https://official.example/a.docx', 'https://official.example/exit?https://other.example/a.pdf'])
def test_file_urls_are_rejected_before_download(url, guard):
    request = Request(url)
    with pytest.raises(IgnoreRequest):
        guard.process_request(request)
    assert request.meta['policy_skipped']


def test_unknown_file_and_split_magic_stop_without_a_complete_body(guard):
    req = Request('https://official.example/download')
    with pytest.raises(StopDownload):
        guard.headers_received(Headers({'Content-Type': 'application/pdf'}), req)
    req = Request('https://official.example/mislabeled')
    guard.bytes_received(b'%P', req)
    with pytest.raises(StopDownload):
        guard.bytes_received(b'DF-1.7', req)
    assert req.meta['policy_skipped']


def test_legacy_attachment_request_is_rejected_even_without_file_extension(guard):
    request = Request('https://official.example/media/1/download', meta={'parsed': {'title': 'old'}})
    with pytest.raises(IgnoreRequest):
        guard.process_request(request)
    assert ignored_retry('attachment_retries', {'attempts': 3})
    assert not ignored_retry('document_retries', {'document': {'url': 'https://official.example/news/1'}})


def test_normalization_preserves_existing_hash_and_sends_only_real_updates(app_env, monkeypatch):
    from alphapulse.crawler import pipelines
    html = '<p>Monetary policy.</p><a href="/cdn-cgi/l/email-protection#abc">Email</a>'
    fields = dict(url='https://official.example/news/1', title='Policy', summary='', content_html=html,
        content_text='Monetary policy.Email', author=None, tags=[], is_paywalled=False,
        published_at=None, source_updated_at=None, extra={})
    news = SimpleNamespace(id=1, revision_no=5, content_hash='legacy-hash', **fields)
    session = AsyncMock()
    session.scalar.return_value = news
    session.add = lambda row: None
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(pipelines, 'SessionLocal', lambda: context)
    publish = AsyncMock()
    monkeypatch.setattr(pipelines, 'redis', SimpleNamespace(publish=publish))
    run = SimpleNamespace(items_seen=0, items_revised=0)
    pipeline = pipelines.NewsPipeline(SimpleNamespace(spider=SimpleNamespace(run=run)))
    def item(content):
        return {**fields, 'source_code': 'fed', 'external_id': 'id', 'content_html': content, 'raw_response_id': None}
    asyncio.run(pipeline.process_item(item(html.replace('abc', 'def'))))
    assert news.revision_no == 5 and news.content_hash == 'legacy-hash'
    publish.assert_not_awaited()
    asyncio.run(pipeline.process_item(item(html.replace('Monetary policy.', 'New interest rate.'))))
    assert news.revision_no == 6 and run.items_revised == 1
    publish.assert_awaited_once()


def test_html_cleanup_keeps_text_after_removed_nodes_and_image_only_articles():
    from alphapulse.core.us_news import article
    assert '保留文字' in clean_html('<script>noise</script>保留文字')
    html = '<time datetime="2026-11-06"></time><article><h1>Policy chart</h1><div class="field--name-body"><img src="/chart.png"><figcaption>利率图</figcaption></div></article>'
    parsed = article('cftc', html, 'https://www.cftc.gov/PressRoom/PressReleases/1-26')
    assert 'https://www.cftc.gov/chart.png' in parsed['content_html']


@pytest.mark.parametrize('source,body', [
    ('sec', '<article class="node--type-regulation"><a href="/files/rules/report.pdf">Final Rule</a></article>'),
    ('fincen', '<main><a href="/system/files/advisory.pdf">Official advisory</a></main>'),
])
def test_file_entry_pages_are_not_mistaken_for_news(source, body):
    from alphapulse.core.us_news import UnsupportedDocument, article
    html = '<h1>Official policy document</h1><time datetime="2026-11-06"></time>' + body
    with pytest.raises(UnsupportedDocument):
        article(source, html, 'https://official.example/document')
