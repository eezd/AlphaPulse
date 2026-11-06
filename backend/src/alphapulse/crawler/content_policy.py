"""新闻只采集网页文字与图片引用；经济日历不使用此策略。"""
import re
from urllib.parse import urljoin, urlsplit

from parsel import Selector

POLICY = 'web_text_images_v1'
SKIP_REASON = '按采集规则不下载 PDF 和附件'
FILE_PATH = re.compile(r'\.(?:pdf|docx?|xlsx?|pptx?|zip|gz|rar|7z|rtf|odt|ods|odp|exe|bin)(?:$|/)', re.I)


def file_url(url):
    parts = urlsplit(url)
    return bool(FILE_PATH.search(parts.path) or re.search(r'\.pdf(?:[?&#]|$)', parts.query, re.I))


def binary_headers(headers):
    kind = headers.get(b'Content-Type', b'').split(b';')[0].strip().lower()
    if not kind:
        return False  # 无类型时由首段 PDF 签名和后续正文解析继续校验。
    return not (kind.startswith(b'text/') or kind in (
        b'application/json', b'application/xml', b'application/rss+xml',
        b'application/atom+xml', b'application/xhtml+xml',
    ) or kind.endswith((b'+json', b'+xml')))



def clean_html(html, base=''):
    """剔除执行/嵌入节点和已确认的动态分享编码，保留正文及图片。"""
    sel = Selector(html.replace('\x00', ''))
    for node in sel.css('script, style, iframe, object, embed, video, audio, .shareDL'):
        parent = node.root.getparent()
        if parent is not None:
            previous, tail = node.root.getprevious(), node.root.tail
            parent.remove(node.root)
            if tail:
                if previous is None:
                    parent.text = (parent.text or '') + tail
                else:
                    previous.tail = (previous.tail or '') + tail
    for node in sel.css('[href*="/cdn-cgi/l/email-protection"]'):
        node.root.attrib.pop('href', None)
    for node in sel.css('[data-cfemail]'):
        node.root.attrib.pop('data-cfemail', None)
    for node in sel.css('img, picture source'):
        if node.root.tag == 'img':
            src = node.attrib.get('data-src') or node.attrib.get('src')
            if src:
                node.root.set('src', urljoin(base, src))
        for attr in ('srcset', 'data-srcset'):
            if value := node.attrib.get(attr):
                candidates = []
                for part in value.split(','):
                    fields = part.strip().split()
                    if fields:
                        candidates.append(' '.join([urljoin(base, fields[0]), *fields[1:]]))
                node.root.set('srcset', ', '.join(candidates))
    # Selector adds an html/body wrapper; keep the original fragment contract.
    body = sel.css('body')
    return ''.join(body[0].xpath('node()').getall()) if body else sel.get()


def ignored_retry(group, value):
    return group == 'attachment_retries' or bool(value.get('skipped_policy')) or file_url(value.get('document', {}).get('url', ''))
