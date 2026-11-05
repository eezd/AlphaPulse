"""US official collectors: serialized documents, bounded history, durable page cursors."""
import copy
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from html import escape, unescape

from lxml import etree
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scrapy import Request
from scrapy.http import Response
from scrapy.spidermiddlewares.httperror import HttpError
from sqlalchemy import select

from alphapulse.calendar.umich import pdf_text
from alphapulse.core.config import get_settings
from alphapulse.core.db import SessionLocal
from alphapulse.core.us_news import RULE_VERSION, SOURCES, article, identity, official_url, published, selection, validate_body
from alphapulse.crawler.spiders.base import HISTORY_RUN_SECONDS, NewsSpider, html_to_text
from alphapulse.models import NewsItem, Source


def configure_url(url, **params):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(params)
    return urlunsplit(parts._replace(query=urlencode(query)))


def congress_bill_url(api_url):
    parts = urlsplit(api_url).path.strip('/').split('/')
    _, _, congress, kind, number = parts
    labels = {'hr': 'house-bill', 's': 'senate-bill', 'hjres': 'house-joint-resolution', 'sjres': 'senate-joint-resolution', 'hconres': 'house-concurrent-resolution', 'sconres': 'senate-concurrent-resolution', 'hres': 'house-resolution', 'sres': 'senate-resolution'}
    return f'https://www.congress.gov/bill/{congress}th-congress/{labels[kind.lower()]}/{number}'


def configuration_error(code):
    settings = get_settings()
    if code == 'congress' and not settings.congress_api_key:
        return '待配置 CONGRESS_API_KEY'
    if code == 'sec' and not settings.crawler_contact_email:
        return '待配置 CRAWLER_CONTACT_EMAIL'
    return None


class USOfficialSpider(NewsSpider):
    """Each lane holds only one page's remaining documents and its next URL."""
    custom_settings = {"DOWNLOAD_MAXSIZE": 25 * 1024 * 1024}

    def make_request_from_data(self, data):
        if getattr(self, '_starting', False):
            return []
        if self.run is not None:
            if not self.crawler.engine.spider_is_idle():
                return super().make_request_from_data(data)
            self._close_run()
        self._starting = True
        self._spawn(self.start_round(json.loads(data)['trigger']))
        return []

    def _close_run(self):
        self._checkpoint_run = self.run
        self._checkpoint_attempts = 1
        super()._close_run()

    async def start_round(self, trigger):
        try:
            if self.finish_task is not None:
                if self.finish_task.done() and not self.finish_task.cancelled() and self.finish_task.exception() is not None and getattr(self, '_checkpoint_run', None) is not None:
                    if self._checkpoint_attempts >= 3:
                        self.logger.error('断点保存连续失败3次，需重启采集进程；未启动下一轮')
                        return
                    self._checkpoint_attempts += 1
                    self.finish_task = self._spawn(self._finish_run(self._checkpoint_run))
                await self.finish_task
            async with SessionLocal() as session:
                source = await session.get(Source, self.name)
                if source is None or not source.enabled:
                    return
                if source.paused_until and source.paused_until > datetime.now(UTC):
                    return
                self.cursor = copy.deepcopy(source.history_cursor or {})
                history = (source.history_target, source.history_position) if source.history_target and not source.history_done else None
                hours = source.backfill_hours
            requests = super().make_request_from_data(json.dumps({'trigger': trigger, 'backfill_hours': hours}).encode())
            self.run.official_cursor = self.cursor
            if history:
                target, position = history
                self.run.history_target = target
                self.run.history_position = position or self.run.started_at
                self.run.history_deadline = time.monotonic() + HISTORY_RUN_SECONDS
                state = self.cursor.setdefault('history', {'entry': 0})
                if state.get('target') != target.isoformat():
                    self.cursor['history'] = {'entry': 0, 'target': target.isoformat()}
                requests += self.advance('history')
            for request in requests:
                self.crawler.engine.crawl(request)
        except Exception:
            self.logger.exception('官方采集轮次启动失败，未使用队列旧断点')
            if self.run is not None:
                self.run.errors.append('官方采集轮次启动失败')
        finally:
            self._starting = False

    def first_requests(self):
        self.run.completed_documents_safe = True
        if self.name == 'doj' and self.cursor.get('transport') != 'doj-api-v1':
            # Retain retry evidence; HTML discovery cursors cannot use API pagination.
            self.cursor = {k: v for k, v in self.cursor.items() if k not in ('history', 'realtime', 'checked')}
            self.cursor['transport'] = 'doj-api-v1'
            self.run.official_cursor = self.cursor
        error = configuration_error(self.name)
        if error:
            self.run.errors.append(error)
            return []
        if self.name == 'federal_register':
            # Existing checkpoints identify completed bodies rather than discovery metadata.
            checked = self.cursor.setdefault('checked', {})
            for url, at in list(checked.items()):
                match = re.fullmatch(r'https://www\.federalregister\.gov/documents/full_text/html/\d{4}/\d{2}/\d{2}/([A-Za-z0-9-]+)\.html', url)
                if match:
                    metadata = f'https://www.federalregister.gov/api/v1/documents/{match[1]}.json'
                    checked[metadata] = max(checked.get(metadata, 0), at)
        if self.name == 'cftc':
            # Earlier discovery treated commissioner indexes as article gaps.
            retries = self.cursor.get('document_retries', {})
            indexes = {url for url in retries if re.fullmatch(r'/PressRoom/SpeechesTestimony/[^/]+/index\.htm', urlsplit(url).path)}
            for url in indexes:
                retries.pop(url)
            for lane in ('realtime', 'history'):
                if 'pending' in self.cursor.get(lane, {}):
                    self.cursor[lane]['pending'] = [d for d in self.cursor[lane]['pending'] if d['url'] not in indexes]
        state = self.cursor.setdefault('realtime', {'entry': 0})
        retries = self.cursor.get('document_retries', {})
        due_documents = [{**value['document'], 'document_retry': True} for value in retries.values() if value['attempts'] < 3 and value['next_at'] <= self.run.started_at.timestamp()]
        if due_documents:
            state.setdefault('pending', []).extend(copy.deepcopy(d) for d in due_documents if d['url'] not in {p['url'] for p in state['pending']})
        due = [value['document'] for value in self.cursor.get('attachment_retries', {}).values() if value['attempts'] < 3 and value['next_at'] <= self.run.started_at.timestamp()]
        for doc in due:
            pending = state.setdefault('pending', [])
            existing = next((value for value in pending if value['url'] == doc['url']), None)
            if existing is None:
                pending.append({**copy.deepcopy(doc), 'attachment_retry': True})
            else:
                existing['attachment_retry'] = True
        bill_due = [{**v['document'], 'bill_retry': True} for v in self.cursor.get('bill_retries', {}).values() if v['attempts'] < 3 and v['next_at'] <= self.run.started_at.timestamp()]
        if bill_due:
            state.setdefault('pending', []).extend(d for d in bill_due if d['url'] not in {p['url'] for p in state['pending']})
        self.run.stats['unresolved_bills'] = len(self.cursor.get('bill_retries', {}))
        return self.advance('realtime')

    def request_for(self, url, callback, lane, **meta):
        url = official_url(self.name, url)
        request = self.request(url, callback, lane=lane, official_source=self.name, **meta)
        request.priority = 10 if lane == 'realtime' else -10
        # Credentials never travel to document hosts or into persisted URLs.
        if urlsplit(url).hostname == 'api.congress.gov':
            request.headers['X-Api-Key'] = get_settings().congress_api_key
        request.headers['User-Agent'] = 'AlphaPulse official news collector ' + get_settings().crawler_contact_email
        return request

    def advance(self, lane):
        state = self.cursor[lane]
        if self.run.blocked:
            return []
        if configuration_error(self.name):
            return []
        if lane == 'history' and time.monotonic() >= self.run.history_deadline:
            return []
        if state.get('pending'):
            doc = state['pending'][0]
            gap = self.cursor.get('document_retries', {}).get(doc['url'])
            if doc['url'] in self.run.document_retries_started or (gap and (gap['attempts'] >= 3 or gap['next_at'] > self.run.started_at.timestamp())):
                state['pending'].pop(0)
                return self.advance(lane)
            if gap:
                doc['document_retry'] = True
                self.run.document_retries_started.add(doc['url'])
            if doc['kind'] == 'bill_finish':
                self.finish_bill(lane)
                return self.advance(lane)
            if doc['kind'] == 'actions' and not state['bill']['reasons']:
                state['pending'].pop(0)
                return self.advance(lane)
            return [self.request_for(doc['url'], self.parse_document, lane, document=doc)]
        if state.get('page_finished'):
            if state.pop('day_complete', False):
                state['day'] = (datetime.fromisoformat(state['day']) - timedelta(days=1)).date().isoformat()
                state['entry'] -= 1
            if state.get('next'):
                state['page'] = state.pop('next')
            else:
                state['entry'] += 1
                state.pop('page', None)
                state.pop('visited_pages', None)
            state.pop('page_finished', None)
        spec = SOURCES[self.name]
        if state['entry'] >= len(spec.entries):
            if lane == 'history':
                self.run.stats['attachment_gaps'] = len(self.cursor.get('attachment_retries', {}))
                self.run.history_done = True
                self.run.history_position = self.run.history_target
                state['scanned'] = True
            else:
                self.cursor['realtime'] = {'entry': 0}
            return []
        url = state.get('page') or spec.entries[state['entry']]
        if self.name == 'congress' and not state.get('page'):
            begin = self.run.history_target if lane == 'history' else self.run.cutoff
            url = configure_url(url, fromDateTime=begin.strftime('%Y-%m-%dT%H:%M:%SZ'), toDateTime=self.run.started_at.strftime('%Y-%m-%dT%H:%M:%SZ'))
        if self.name == 'federal_register' and not state.get('page'):
            begin = self.run.history_target if lane == 'history' else self.run.cutoff
            # Daily partitions stay below the API's 2,000-result pagination ceiling.
            day = state.setdefault('day', self.run.started_at.date().isoformat())
            if day < begin.date().isoformat():
                state['entry'] += 1
                return self.advance(lane)
            url = configure_url(url, **{'conditions[publication_date][is]': day, 'per_page': '1000', 'order': 'oldest'})
        state['page'] = url
        return [self.request_for(url, self.parse_listing, lane)]

    def parse_listing(self, response):
        lane = response.meta['lane']
        state = self.cursor[lane]
        docs, next_url = [], None
        if self.name == 'doj':
            data = response.json()
            resultset = data['metadata']['resultset']
            if data['metadata']['responseInfo']['status'] != 200 or not isinstance(data['results'], list):
                raise ValueError('DOJ API 列表结构未识别')
            dates = []
            for record in data['results']:
                dates.append(datetime.fromtimestamp(int(record['date']), UTC))
                docs.append({'url': f"https://www.justice.gov/api/v1/press_releases/{uuid.UUID(record['uuid'])}.json", 'kind': 'doj'})
            page, size, count = (int(resultset[k]) for k in ('page', 'pagesize', 'count'))
            if size <= 0 or page < 0 or (not docs and page * size < count):
                raise ValueError('DOJ API 分页结果不完整')
            begin = self.run.history_target if lane == 'history' else self.run.cutoff
            if (page + 1) * size < count and not (dates and all(d.date() < begin.date() for d in dates)):
                next_url = configure_url(response.url, page=page + 1)
        elif self.name == 'fed' and response.url.endswith('releaseDates.json'):
            data = response.json()
            if not isinstance(data, list) or not data:
                raise ValueError('H.4.1 日期列表结构未识别')
            docs = [{'url': f'https://www.federalreserve.gov/releases/h41/{day}/', 'kind': 'article'} for year in data for month in year['Months'] for day in month['Dates'] if int(year['yearValue']) >= 2025]
            docs.sort(key=lambda d: d['url'], reverse=True)
        elif self.name in ('congress', 'federal_register'):
            data = response.json()
            key = ('committeeMeetings' if '/committee-meeting' in response.url else 'bills') if self.name == 'congress' else 'results'
            if self.name == 'federal_register' and key not in data and type(data.get('count')) is int and data['count'] == 0 and isinstance(data.get('description'), str):
                data[key] = []
            if key not in data or not isinstance(data[key], list):
                raise ValueError('官方 API 列表结构未识别')
            if self.name == 'federal_register' and data.get('count', 0) > 2000:
                day = state['day']
                url = f'https://www.govinfo.gov/content/pkg/FR-{day}/xml/FR-{day}.xml'
                state['pending'] = [{'url': url, 'kind': 'register_issue', 'day': day, 'count': data['count']}]
                yield from self.advance(lane)
                return
            for doc in data[key]:
                if self.name == 'congress':
                    docs.append({'url': configure_url(doc['url'], format='json'), 'kind': 'meeting' if key == 'committeeMeetings' else 'bill'})
                else:
                    docs.append({'url': f"https://www.federalregister.gov/api/v1/documents/{doc['document_number']}.json", 'kind': 'register'})
            next_url = data.get('pagination', {}).get('next') if self.name == 'congress' else data.get('next_page_url')
        else:
            heading = ' '.join(response.css('title, h1').xpath('normalize-space(.)').getall())
            if re.search(r'just a moment|access denied|verify (?:you are|your)|captcha', heading, re.I) or response.css('#challenge-form, #cf-challenge-running'):
                self.block_until(datetime.now(UTC) + timedelta(minutes=15))
                raise ValueError('官方列表返回验证页面')
            if not response.css('h1'):
                raise ValueError('官方列表页面未识别')
            pattern = SOURCES[self.name].article_path
            for a in response.css('a[href]'):
                try:
                    url = official_url(self.name, a.attrib['href'], response.url)
                except ValueError:
                    continue
                fincen_row = a.xpath('ancestor::tr[ancestor::main][1]') if self.name == 'fincen' and response.url.split('?')[0] in SOURCES['fincen'].entries[1:] else []
                fincen_pdf = bool(fincen_row and urlsplit(url).path.lower().endswith('.pdf'))
                if fincen_pdf or (re.search(pattern, urlsplit(url).path) and url not in SOURCES[self.name].entries and not re.search(r'/\d{4}-(?:press|speeches|testimony)\.htm$', urlsplit(url).path) and not url.lower().endswith('.pdf')):
                    doc = {'url': url, 'kind': 'article'}
                    if fincen_row:
                        # Table times use noon as a date marker, not a release instant.
                        date = fincen_row.xpath('normalize-space(td[2])').get()
                        begin = self.run.history_target if lane == 'history' else self.run.cutoff
                        if re.fullmatch(r'(?:0?[1-9]|1[0-2])/\d{4}', date) and int(date[-4:]) < begin.year:
                            continue
                        doc['listing_date'] = published(date)[0].date().isoformat()
                        if fincen_pdf:
                            title = fincen_row.xpath('normalize-space(td[3])').get() if response.url.split('?')[0] == SOURCES['fincen'].entries[2] else a.xpath('normalize-space(.)').get()
                            if not title:
                                raise ValueError('FinCEN PDF 列表缺少标题')
                            doc.update(format='pdf', title=title, listing_url=response.url)
                    elif self.name in ('treasury', 'cftc', 'fincen', 'whitehouse'):
                        container = {
                            'treasury': 'ancestor::div[h3][1]',
                            'cftc': 'ancestor::tr[1]',
                            'fincen': 'ancestor::div[contains(@class,"fincen-news-article__content")][1]',
                            'whitehouse': 'ancestor::div[contains(@class,"wp-block-whitehouse-post-template__content")][1]',
                        }[self.name]
                        dates = a.xpath(container + '//time/@datetime').getall()
                        if len(dates) == 1:
                            doc['listing_date'] = published(dates[0])[0].isoformat()
                    if self.name == 'fed' and '/monetarypolicy/fomcminutes' in url:
                        # The URL identifies the meeting; the calendar gives the release day.
                        match = re.search(r'Released\s+([A-Za-z]+ \d{1,2}, \d{4})', a.xpath('normalize-space(parent::*)').get() or '')
                        if not match:
                            raise ValueError('FOMC 纪要链接缺少官方发布日期')
                        doc.update(release_date=published(match[1])[0].date().isoformat(), release_date_source=response.url)
                    docs.append(doc)
                if a.attrib.get('rel') == 'next' or re.match(r'^(?:Next|Next page|下一页)', a.xpath('normalize-space(.)').get() or '', re.I):
                    next_url = url
            docs = list({d['url']: d for d in docs}.values())
            if not docs:
                raise ValueError('官方列表未发现文档，不能认定历史遍历完成')
        if self.name == 'fed':
            begin = self.run.history_target if lane == 'history' else self.run.cutoff
            def in_range(doc):
                if doc.get('release_date'):
                    day = published(doc['release_date'])[0].date()
                elif match := re.search(r'((?:19|20)\d{6})', doc['url']):
                    day = datetime.strptime(match[1], '%Y%m%d').date()
                else:
                    return True
                return day >= begin.date()
            docs = [doc for doc in docs if in_range(doc)]
        if self.name in ('treasury', 'cftc', 'fincen', 'whitehouse') and docs:
            begin = self.run.history_target if lane == 'history' else self.run.cutoff
            dates = [published(d['listing_date'])[0] for d in docs if d.get('listing_date')]
            if len(dates) == len(docs) and dates == sorted(dates, reverse=True) and dates[-1].date() < begin.date():
                next_url = None
            docs = [d for d in docs if not d.get('listing_date') or published(d['listing_date'])[0].date() >= begin.date()]
        if lane == 'realtime':
            # OFAC's recent-actions entry is a descending day-indexed feed.
            if self.name == 'ofac' and docs:
                days = [re.search(r'/recent-actions/(\d{8})', d['url']) for d in docs]
                if all(days) and all(m[1] < self.run.cutoff.strftime('%Y%m%d') for m in days):
                    next_url = None
            checked = self.cursor.setdefault('checked', {})
            threshold = (self.run.started_at - timedelta(hours=24)).timestamp()
            self.cursor['checked'] = {k: v for k, v in checked.items() if v >= threshold}
            docs = [d for d in docs if self.cursor['checked'].get(d['url'], 0) < threshold]
        retries = self.cursor.get('document_retries', {})
        docs = [d for d in docs if d['url'] not in retries or (retries[d['url']]['attempts'] < 3 and retries[d['url']]['next_at'] <= self.run.started_at.timestamp())]
        docs = [{**d, 'document_retry': True} if d['url'] in retries else d for d in docs]
        if next_url and official_url(self.name, next_url, response.url) == response.url:
            raise ValueError('官方列表翻页重复当前页面，未推进断点')
        visited = state.setdefault('visited_pages', [])
        if response.url in visited:
            raise ValueError('官方列表分页出现循环，未推进断点')
        visited.append(response.url)
        if next_url and official_url(self.name, next_url, response.url) in visited:
            raise ValueError('官方列表分页出现循环，未推进断点')
        state['pending'] = docs
        state['next'] = official_url(self.name, next_url, response.url) if next_url else None
        state['page_finished'] = True
        self.run.stats['discovered'] = self.run.stats.get('discovered', 0) + len(docs)
        if self.name == 'federal_register' and not next_url:
            state['day_complete'] = True
        yield from self.advance(lane)

    def document_failed(self, request, error):
        doc = request.meta.get('document')
        if not doc or doc['kind'] not in ('article', 'doj'):
            return None
        state = self.cursor[request.meta['lane']]
        if not state.get('pending') or state['pending'][0]['url'] != doc['url']:
            return None
        retries = self.cursor.setdefault('document_retries', {})
        previous = retries.get(doc['url'], {})
        now = datetime.now(UTC)
        if not previous or previous['next_at'] <= now.timestamp():
            retries[doc['url']] = {'document': copy.deepcopy(doc), 'attempts': previous.get('attempts', 0) + 1,
                                  'next_at': (now + timedelta(hours=1)).timestamp(), 'error': error}
        self.run.document_retries_started.add(doc['url'])
        self.run.errors.append(error)
        self.logger.error('文档缺口：%s；%s', doc['url'], error)
        state['pending'].pop(0)
        return self.advance(request.meta['lane'])

    def on_request_error(self, failure):
        status = failure.value.response.status if failure.check(HttpError) else None
        if status not in (403, 429):
            requests = self.document_failed(failure.request, f'请求失败 {failure.request.url}：{status or repr(failure.value)}')
            if requests is not None:
                return requests
        return super().on_request_error(failure)

    async def parse_document(self, response):
        try:
            async for result in self._parse_document(response):
                yield result
        except (ValueError, KeyError, TypeError) as exc:
            if '验证' in str(exc) or '登录页面' in str(exc):
                self.block_until(datetime.now(UTC) + timedelta(minutes=15))
            requests = self.document_failed(response.request, f'解析异常 {response.url}：{exc!r}')
            if requests is None:
                raise
            for request in requests:
                yield request

    async def _parse_document(self, response):
        lane, doc = response.meta['lane'], response.meta['document']
        begin = self.run.history_target if lane == 'history' else self.run.cutoff
        if not (doc.get('document_retry') or doc.get('attachment_retry')) and doc.get('listing_date') and published(doc['listing_date'])[0].date() < begin.date():
            self.run.stats['outside_range'] = self.run.stats.get('outside_range', 0) + 1
            for result in self.complete(lane):
                yield result
            return
        if doc['kind'] == 'register_issue':
            root = etree.fromstring(response.body, etree.XMLParser(resolve_entities=False, no_network=True))
            if root.tag != 'FEDREG' or published(root.findtext('DATE', default=''))[0].date().isoformat() != doc['day']:
                raise ValueError('Federal Register XML 日期或结构不符')
            numbers = set()
            for node in root.iter('FRDOC'):
                match = re.search(r'FR Doc\.\s+([^\s\]]+)', ''.join(node.itertext()))
                if not match:
                    raise ValueError('Federal Register XML 文档编号无法解析')
                numbers.add(match[1])
            if len(numbers) != doc['count']:
                raise ValueError('Federal Register XML 文档数量与 API 不符')
            state = self.cursor[lane]
            state['pending'] = [{'url': f'https://www.federalregister.gov/api/v1/documents/{number}.json', 'kind': 'register'} for number in sorted(numbers)]
            state.update(page_finished=True, next=None, day_complete=True)
            self.run.stats['discovered'] = self.run.stats.get('discovered', 0) + len(numbers)
            for result in self.advance(lane):
                yield result
            return
        if doc['kind'] == 'meeting':
            data = response.json()['committeeMeeting']
            at, precision = published(data['updateDate'])
            if not selection(self.name, data['title'], json.dumps(data.get('relatedItems', {}))):
                self.run.stats['excluded'] = self.run.stats.get('excluded', 0) + 1
                for result in self.complete(lane):
                    yield result
                return
            parsed = {'title': data['title'], 'content_html': '<pre>' + escape(json.dumps(data, ensure_ascii=False, indent=2)) + '</pre>', 'published_at': at, 'url': response.url, 'external_id': identity(response.url), 'raw_response_id': response.meta['raw_response_id'], 'retry_document': doc,
                      'extra': {'content_scope': 'article', 'published_at_precision': precision, 'published_at_basis': 'official_update', 'published_date': at.date().isoformat(), 'official_meeting': data, 'rule_version': RULE_VERSION, 'attachments': [{'url': official_url(self.name, d['url']), 'title': d.get('name') or d.get('documentType', ''), 'status': 'pending'} for d in {d['url']: d for d in data.get('meetingDocuments', []) + data.get('witnessDocuments', []) if d.get('format') == 'PDF'}.values()]}}
            for result in self.attach_or_finish(parsed, lane):
                yield result
            return
        if doc['kind'] == 'bill':
            data = response.json()['bill']
            base = response.url.split('?')[0]
            state = self.cursor[lane]
            previous = self.cursor.setdefault('bill_retries', {}).get(base)
            version = data.get('updateDateIncludingText') or data.get('updateDate')
            if previous and previous.get('version') != version:
                self.cursor['bill_retries'].pop(base)
            if previous and previous.get('version') == version and (previous['attempts'] >= 3 or previous['next_at'] > self.run.started_at.timestamp()):
                for result in self.complete(lane):
                    yield result
                return
            reasons = selection(self.name, data['title'], json.dumps(data.get('policyArea', {})))
            state['bill'] = {'url': base, 'document_url': doc['url'], 'title': data['title'], 'reasons': reasons, 'bodies': 0, 'version': version, 'retry': bool(doc.get('bill_retry') or doc.get('attachment_retry'))}
            kinds = ([] if reasons else ['subjects', 'summaries']) + ['texts', 'actions', 'bill_finish']
            state['pending'][0:1] = [{'url': base + '/' + ('text' if kind == 'texts' else kind) + '?format=json&limit=250', 'kind': kind, 'title': data['title'], 'bill': base} for kind in kinds]
            for result in self.advance(lane):
                yield result
            return
        if doc['kind'] in ('subjects', 'summaries'):
            data = response.json()
            key = 'subjects' if doc['kind'] == 'subjects' else 'summaries'
            if key not in data:
                raise ValueError('官方法案补充资料结构未识别')
            state = self.cursor[lane]
            state['bill']['reasons'] = state['bill']['reasons'] or selection(self.name, doc['title'], json.dumps(data[key]))
            children = []
            if next_url := data.get('pagination', {}).get('next'):
                children.append({**doc, 'url': official_url(self.name, next_url)})
            state['pending'][0:1] = children
            for result in self.advance(lane):
                yield result
            return
        if doc['kind'] in ('actions', 'texts'):
            data = response.json()
            children = []
            if doc['kind'] == 'actions':
                for action in data['actions']:
                    key = identity(doc['bill'] + json.dumps(action, sort_keys=True))
                    at, precision = published(action['actionDate'])
                    begin = datetime(2025, 1, 1, tzinfo=UTC) if self.cursor[lane]['bill'].get('retry') else (self.run.history_target if lane == 'history' else self.run.cutoff)
                    if at < begin:
                        continue
                    item = self.item(response, external_id=key, url=congress_bill_url(doc['bill']), title=doc['title'], summary='', content_html=f"<p>{escape(action['text'])}</p>", published_at=at,
                                     extra={'content_scope': 'official_action', 'published_at_precision': precision, 'published_date': at.date().isoformat(), 'bill': doc['bill'], 'official_action': action, 'rule_version': RULE_VERSION})
                    yield item
            else:
                for version in data['textVersions']:
                    if not version.get('date'):
                        self.cursor[lane]['bill']['missing_material'] = True
                        self.cursor[lane]['bill'].setdefault('material_errors', []).append(f"{version.get('type', '正文版本')} 缺少官方日期")
                        continue
                    formats = version.get('formats', [])
                    chosen = next((f for f in formats if f['type'] == 'Formatted Text'), None) or next((f for f in formats if f['type'] == 'Formatted XML'), None) or next((f for f in formats if f['type'] == 'PDF'), None)
                    if not chosen:
                        self.cursor[lane]['bill']['missing_material'] = True
                        self.cursor[lane]['bill'].setdefault('material_errors', []).append(f"{version.get('type', '正文版本')} 缺少支持的正文格式")
                    if chosen:
                        children.append({'url': chosen['url'], 'kind': 'text', 'title': doc['title'] + ' — ' + version['type'], 'at': version['date'], 'bill': doc['bill']})
            next_url = data.get('pagination', {}).get('next')
            if next_url:
                children.append({**doc, 'url': official_url(self.name, next_url)})
            self.cursor[lane]['pending'][0:1] = children
            for result in self.advance(lane):
                yield result
            return
        if doc['kind'] == 'register':
            data = response.json()
            url = data.get('body_html_url') or data.get('raw_text_url')
            if not url:
                raise ValueError('规则文档缺少正文地址')
            self.cursor[lane]['pending'][0] = {'url': official_url(self.name, url), 'kind': 'text', 'title': data['title'], 'at': data['publication_date'], 'canonical': data['html_url'], 'document_number': data['document_number'], 'effective_on': data.get('effective_on'), 'pdf': data.get('pdf_url'), 'metadata_url': response.url}
            for result in self.advance(lane):
                yield result
            return
        if doc['kind'] == 'doj':
            data = response.json()
            records = data['results']
            if data['metadata']['responseInfo']['status'] != 200 or len(records) != 1:
                raise ValueError('DOJ API 正文结构未识别')
            record = records[0]
            if str(uuid.UUID(record['uuid'])) != doc['url'].rsplit('/', 1)[1].removesuffix('.json'):
                raise ValueError('DOJ API 正文 UUID 不符')
            canonical = official_url(self.name, record['url'].replace('https://justice.gov/', 'https://www.justice.gov/', 1))
            title = unescape(record['title']).strip()
            if not title:
                raise ValueError('DOJ 正文缺少标题')
            # The API release date is a calendar date, not a release instant.
            date = datetime.fromtimestamp(int(record['date']), UTC).date().isoformat()
            attachment_html = record.get('attachment') or ''
            api_attachments = []
            if isinstance(attachment_html, list):
                for attachment in attachment_html:
                    file_id = attachment['file']['id']
                    if not str(file_id).isdigit():
                        raise ValueError('DOJ API 附件编号无效')
                    api_attachments.append({'url': f'https://www.justice.gov/media/{file_id}/dl', 'title': attachment.get('description') or 'Official attachment', 'status': 'pending'})
                attachment_html = ''
            elif not isinstance(attachment_html, str):
                raise ValueError('DOJ API 附件结构未识别')
            html = f'<h1>{escape(title)}</h1><time datetime="{date}"></time><article><div class="field--name-body">{record["body"]}{attachment_html}</div></article>'
            validate_body(record['body'])
            parsed = article(self.name, html, canonical)
            known = {a['url'] for a in parsed['extra']['attachments']}
            parsed['extra']['attachments'].extend(a for a in api_attachments if a['url'] not in known)
            changed = etree.HTML(record.get('changed') or '')
            if changed is not None:
                times = changed.xpath('//time/@datetime')
                if times:
                    parsed['source_updated_at'] = published(times[0])[0]
            parsed['extra']['official_uuid'] = record['uuid']
            doc = {**doc, 'canonical': canonical}
        elif doc['kind'] == 'text' or doc.get('format') == 'pdf':
            at, precision = published(doc['listing_date'] if doc.get('format') == 'pdf' else doc['at'])
            if response.body.startswith(b'%PDF'):
                content = '<pre>' + escape(pdf_text(response.body)) + '</pre>'
            else:
                content = response.text
                validate_body(content)
                if doc.get('format') == 'pdf':
                    raise ValueError('官方 PDF 返回非 PDF 正文')
                if '<' not in content:
                    content = '<pre>' + escape(content) + '</pre>'
            parsed = {'title': doc['title'], 'content_html': content, 'published_at': at, 'extra': {'content_scope': 'article', 'language': 'en', 'published_at_precision': precision, 'published_date': at.date().isoformat(), 'rule_version': RULE_VERSION, 'official_document': doc, 'attachments': []}}
            if doc.get('format') == 'pdf':
                parsed['extra'].update(published_at_basis='official_listing', published_at_source=doc['listing_url'])
            if doc.get('pdf'):
                parsed['extra']['attachments'].append({'url': official_url(self.name, doc['pdf']), 'title': 'Official PDF', 'status': 'pending'})
        else:
            parsed = article(self.name, response.text, response.url, release_date=doc.get('release_date'))
            if doc.get('release_date'):
                parsed['extra'].update(published_at_basis='official_calendar_release', published_at_source=doc['release_date_source'])
        if doc.get('bill'):
            context = self.cursor[lane]['bill']
            context['bodies'] += 1
            if not context['reasons']:
                context['reasons'] = selection(self.name, context['title'], html_to_text(parsed['content_html']))
                if context['reasons']:
                    # A later version can establish relevance for earlier versions.
                    self.cursor[lane]['pending'][1:1] = context.pop('unmatched_texts', [])
                else:
                    context.setdefault('unmatched_texts', []).append(copy.deepcopy(doc))
            parsed['extra']['selection_reasons'] = context['reasons']
        begin = self.run.history_target if lane == 'history' else self.run.cutoff
        if doc.get('attachment_retry') or doc.get('document_retry') or (doc.get('bill') and self.cursor[lane]['bill'].get('retry')):
            begin = datetime(2025, 1, 1, tzinfo=UTC)
        outside_range = parsed['published_at'].date() < begin.date() if parsed['extra'].get('published_at_precision') == 'date' else parsed['published_at'] < begin
        if outside_range:
            self.run.stats['outside_range'] = self.run.stats.get('outside_range', 0) + 1
            for result in self.complete(lane):
                yield result
            return
        if lane == 'history':
            self.run.history_position = min(self.run.history_position or self.run.started_at, parsed['published_at'])
        parsed['url'] = doc.get('canonical') or response.url
        parsed['external_id'] = identity(parsed['url'])
        if self.name == 'fed':
            # Preserve pre-existing RSS GUID identities during full-text enrichment.
            async with SessionLocal() as session:
                existing = await session.scalar(select(NewsItem.external_id).where(NewsItem.source_code == 'fed', NewsItem.url == parsed['url']).limit(1))
                if existing:
                    parsed['external_id'] = existing
        parsed['raw_response_id'] = response.meta['raw_response_id']
        parsed['retry_document'] = {'url': doc['bill'], 'kind': 'bill'} if doc.get('bill') else doc
        for result in self.attach_or_finish(parsed, lane):
            yield result

    def finish_bill(self, lane):
        context = self.cursor[lane].pop('bill')
        retries = self.cursor.setdefault('bill_retries', {})
        if not context['bodies'] or context.get('missing_material'):
            previous = retries.get(context['url'], {})
            errors = list(context.get('material_errors', []))
            if not context['bodies']:
                errors.append('没有可用的官方正文')
            retries[context['url']] = {'document': {'url': context['url'], 'kind': 'bill'}, 'version': context['version'], 'attempts': previous.get('attempts', 0) + 1, 'next_at': (datetime.now(UTC) + timedelta(hours=1)).timestamp(), 'error': '；'.join(dict.fromkeys(errors)) or '官方正文材料不完整'}
        else:
            retries.pop(context['url'], None)
            if not context['reasons']:
                self.run.stats['excluded'] = self.run.stats.get('excluded', 0) + 1
        self.run.stats['unresolved_bills'] = len(retries)
        self.cursor[lane]['pending'].pop(0)
        if lane == 'realtime':
            self.cursor.setdefault('checked', {})[context['document_url']] = self.run.started_at.timestamp()

    def attach_or_finish(self, parsed, lane):
        pending = next((a for a in parsed['extra']['attachments'] if a['status'] == 'pending'), None)
        if pending:
            request = self.request_for(pending['url'], self.parse_attachment, lane, parsed=parsed)
            request.meta['handle_httpstatus_list'] = [404, 410]
            yield request
            return
        text = html_to_text(parsed['content_html'])
        reasons = selection(self.name, parsed['title'], text) or parsed['extra'].get('selection_reasons', [])
        incomplete = any(a['status'] != 'complete' for a in parsed['extra']['attachments'])
        retry_doc = parsed.pop('retry_document', None)
        retries = self.cursor.setdefault('attachment_retries', {})
        if incomplete and retry_doc:
            previous = retries.get(parsed['url'], {})
            error = '；'.join(f"{a['url']}：{a.get('error') or '附件尚未完成文字提取'}" for a in parsed['extra']['attachments'] if a['status'] != 'complete')
            retries[parsed['url']] = {'document': retry_doc, 'attempts': previous.get('attempts', 0) + 1, 'next_at': (datetime.now(UTC) + timedelta(hours=1)).timestamp(), 'error': error}
        elif not incomplete:
            retries.pop(parsed['url'], None)
        if reasons:
            parsed['extra']['selection_reasons'] = reasons
            parsed['extra']['content_scope'] = 'partial' if incomplete else 'article'
            self.run.stats['partial'] = self.run.stats.get('partial', 0) + int(incomplete)
            raw_id = parsed.pop('raw_response_id')
            item = self.item(Response(parsed['url'], request=Request(parsed['url'], meta={'raw_response_id': raw_id})), summary='', **parsed)
            yield item
        else:
            self.run.stats['excluded'] = self.run.stats.get('excluded', 0) + 1
            if incomplete:
                self.run.stats['unclassified_attachment_gaps'] = self.run.stats.get('unclassified_attachment_gaps', 0) + 1
        yield from self.complete(lane)

    def parse_attachment(self, response):
        parsed = response.meta['parsed']
        attachment = next(a for a in parsed['extra']['attachments'] if a['url'] == response.url or a['status'] == 'pending')
        attachment['raw_response_id'] = response.meta['raw_response_id']
        try:
            if response.status != 200:
                raise ValueError(f'HTTP {response.status}')
            if response.headers.get('Content-Type', b'').lower().startswith(b'image/') and response.body.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a')):
                raise ValueError('官方图片附件尚未提取文字，请核对原文')
            text = pdf_text(response.body)
            attachment['status'] = 'complete'
            parsed['content_html'] += f"<section><h2>{escape(attachment['title'])}</h2><pre>{escape(text)}</pre></section>"
        except (ValueError, OSError) as exc:
            attachment.update(status='unavailable', error=str(exc))
            self.logger.warning('附件缺口：%s', response.url)
        yield from self.attach_or_finish(parsed, response.meta['lane'])

    def complete(self, lane):
        doc = self.cursor[lane]['pending'].pop(0)
        self.cursor.get('document_retries', {}).pop(doc['url'], None)
        if lane == 'realtime':
            checked = self.cursor.setdefault('checked', {})
            checked[doc['url']] = self.run.started_at.timestamp()
            if doc.get('metadata_url'):
                checked[doc['metadata_url']] = self.run.started_at.timestamp()
        return self.advance(lane)


class SecSpider(USOfficialSpider):
    name = 'sec'


class CftcSpider(USOfficialSpider):
    custom_settings = {**USOfficialSpider.custom_settings, 'DOWNLOAD_DELAY': 3.0, 'CONCURRENT_REQUESTS': 1}
    name = 'cftc'


class TreasurySpider(USOfficialSpider):
    name = 'treasury'


class OfacSpider(USOfficialSpider):
    name = 'ofac'


class FincenSpider(USOfficialSpider):
    name = 'fincen'


class WhitehouseSpider(USOfficialSpider):
    name = 'whitehouse'


class CongressSpider(USOfficialSpider):
    name = 'congress'


class FederalRegisterSpider(USOfficialSpider):
    name = 'federal_register'


class OccSpider(USOfficialSpider):
    name = 'occ'


class DojSpider(USOfficialSpider):
    custom_settings = {**USOfficialSpider.custom_settings, 'DOWNLOAD_DELAY': 1.0}
    name = 'doj'
