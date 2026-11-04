"""Capture bounded, resumable Congress acceptance evidence without production I/O."""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

CREDENTIAL_FIELDS = {'apikey', 'api_key', 'token', 'key'}


def clean_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in CREDENTIAL_FIELDS]
    return urlunsplit(parts._replace(query=urlencode(query)))


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k.lower() not in CREDENTIAL_FIELDS}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and value.startswith('https://'):
        return clean_url(value)
    return value


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with gzip.open(temporary, 'wt') as file:
        json.dump(data, file)
    temporary.replace(path)


class CaptureBlocked(RuntimeError):
    pass


class Capture:
    def __init__(self, path, key, limit=60, client=None):
        self.path, self.key, self.limit = path, key, limit
        self.client = client or httpx.Client(timeout=25, follow_redirects=False)
        self.records = json.load(gzip.open(path, 'rt')) if path.exists() else {}
        self.requests = 0
        self.used_urls = set()

    def fetch(self, url):
        url = clean_url(url)
        from alphapulse.core.us_news import official_url
        official_url('congress', url)
        self.used_urls.add(url)
        if url in self.records:
            return base64.b64decode(self.records[url]['body'])
        if self.requests >= self.limit:
            raise CaptureBlocked('本轮请求预算耗尽；成功响应已保存，下次可恢复')
        self.requests += 1
        headers = {'X-Api-Key': self.key} if urlsplit(url).hostname == 'api.congress.gov' else {}
        response = self.client.get(url, headers=headers)
        if response.status_code == 429:
            raise CaptureBlocked(f"HTTP 429；Retry-After={response.headers.get('retry-after', 'unknown')}；已停止请求")
        if response.status_code != 200:
            raise CaptureBlocked(f'HTTP {response.status_code}；未保存错误响应')
        body = response.content
        if 'json' in response.headers.get('content-type', ''):
            body = json.dumps(redact(response.json())).encode()
        self.records[url] = {'body': base64.b64encode(body).decode()}
        save(self.path, self.records)
        time.sleep(1)
        return body


async def run(capture, item_sink=None, meeting_url=None):
    # Override every application I/O setting before importing the parser modules.
    os.environ.update(DATABASE_URL='postgresql+asyncpg://test:test@127.0.0.1:1/test', REDIS_URL='redis://127.0.0.1:1/0', JWT_SECRET='test-only', ADMIN_USERNAME='test', SECRET_ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import patch
    import uuid
    from scrapy import Request
    from scrapy.http import Response
    from alphapulse.crawler.spiders import us_official
    from alphapulse.crawler.spiders.base import RunState
    from alphapulse.core.us_news import CRYPTO
    report = {'accepted': False, 'cases': [], 'requests': 0}
    roots = [('GENIUS', 'bill/119/s/1582'), ('CLARITY', 'bill/119/hr/3633'), ('STABLE', 'bill/119/hr/2392'), ('meeting', 'committee-meeting/119')]
    with patch.object(us_official, 'get_settings', lambda: SimpleNamespace(congress_api_key=capture.key, crawler_contact_email='test@example.invalid')):
        for label, route in roots:
            obj = us_official.USOfficialSpider(name='congress')
            now = datetime.now(UTC)
            obj.run = RunState(uuid.uuid4(), 'schedule', now, now, history_target=datetime(2025,1,1,tzinfo=UTC), history_deadline=float('inf'))
            url = f'https://api.congress.gov/v3/{route}?format=json&limit=250'
            obj.cursor = {'history': {'entry': 0, 'pending': []}}
            case = {'name': label, 'items': 0, 'article_items': 0, 'partial_items': 0, 'complete': False}
            report['cases'].append(case)
            try:
                if label == 'meeting' and meeting_url:
                    from alphapulse.core.us_news import official_url
                    selected = official_url('congress', meeting_url)
                    if urlsplit(selected).hostname != 'api.congress.gov' or '/committee-meeting/119/' not in urlsplit(selected).path:
                        raise CaptureBlocked('会议地址必须是119届Congress API单场会议地址')
                    obj.cursor['history']['pending'] = [{'url': us_official.configure_url(selected, format='json'), 'kind': 'meeting'}]
                elif label == 'meeting':
                    listing = json.loads(capture.fetch(url))
                    obj.cursor['history']['pending'] = [{'url': us_official.configure_url(entry['url'], format='json'), 'kind': 'meeting'} for entry in listing['committeeMeetings']]
                    if not obj.cursor['history']['pending']:
                        raise CaptureBlocked('会议列表为空，不能认定验收完成')
                else:
                    obj.cursor['history']['pending'] = [{'url':url, 'kind':'bill'}]
                request = None
                while obj.cursor['history']['pending']:
                    if request is None:
                        request = obj.advance('history')[0]
                    body = capture.fetch(request.url)
                    if label == 'meeting' and request.callback == obj.parse_document:
                        meeting = json.loads(body)['committeeMeeting']
                        if not CRYPTO.search(meeting['title'] + json.dumps(meeting.get('relatedItems', {}))):
                            obj.cursor['history']['pending'].pop(0)
                            request = None
                            continue
                    request.meta['raw_response_id'] = None
                    response = Response(request.url, body=body, request=request)
                    if request.callback == obj.parse_attachment:
                        output = list(obj.parse_attachment(response))
                    else:
                        from scrapy.http import HtmlResponse
                        output = [v async for v in obj.parse_document(HtmlResponse(response.url, body=body, encoding='utf-8', request=request))]
                    requests = [v for v in output if isinstance(v, Request) and v.meta.get('document')]
                    # Attachment requests hold parsed state rather than a document.
                    requests += [v for v in output if isinstance(v, Request) and v.meta.get('parsed')]
                    for item in (v for v in output if not isinstance(v, Request)):
                        if item_sink is not None:
                            item_sink.append(dict(item))
                        case['items'] += 1
                        case['article_items'] += int(item['extra']['content_scope'] == 'article')
                        case['partial_items'] += int(item['extra']['content_scope'] == 'partial')
                    if label == 'meeting' and case['article_items']:
                        break
                    request = requests[0] if requests else None
                    if request is None and not obj.cursor['history']['pending']:
                        break
                case['traversal_complete'] = not obj.cursor['history']['pending'] if label != 'meeting' else bool(case['article_items'] + case['partial_items'])
                case['parser_accepted'] = case['traversal_complete'] and bool(case['article_items'] + case['partial_items'])
                case['unresolved_bills'] = obj.run.stats.get('unresolved_bills', 0)
                case['complete'] = bool(case['article_items']) and not case['unresolved_bills'] and not obj.cursor.get('attachment_retries')
            except Exception as exc:
                # Error text may contain a credential-bearing upstream URL.
                case['error'] = str(exc) if isinstance(exc, CaptureBlocked) else type(exc).__name__
                break
    report['requests'] = capture.requests
    report['accepted'] = len(report['cases']) == len(roots) and all(c['complete'] for c in report['cases'])
    return report


def main():
    import asyncio
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--meeting-url', help='Official Congress API URL for a known crypto hearing')
    parser.add_argument('--max-requests', type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 100:
        parser.error('--max-requests must be between 1 and 100')
    capture = Capture(args.cache, os.environ.get('CONGRESS_TEST_API_KEY') or 'DEMO_KEY', args.max_requests)
    try:
        report = asyncio.run(run(capture, meeting_url=args.meeting_url))
        save(args.cache.with_suffix('.report.gz'), report)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        capture.client.close()
    raise SystemExit(0 if report['accepted'] else 1)


if __name__ == '__main__':
    main()
