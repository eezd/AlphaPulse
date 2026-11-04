"""Read-only research tools with bounded calls and durable source snapshots."""
import asyncio
import ipaddress
import json
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit
import httpx
from parsel import Selector
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from alphapulse.ai import llm
from alphapulse.core.db import SessionLocal
from alphapulse.models import NewsItem
from alphapulse.models.risk import NewsSummary
from alphapulse.news_risk.service import fits_context, validate_submission, model_stream


async def public_address(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('只允许公开HTTP/HTTPS地址')
    answers = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    addresses = [item[4][0] for item in answers]
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('不允许访问非公开地址')
    return parsed, addresses[0]


async def fetch_public_response(url):
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        for _ in range(4):
            parsed, address = await public_address(url)
            # Pin the checked address while retaining TLS hostname verification.
            target = httpx.URL(url).copy_with(host=address)
            async with client.stream('GET', target, headers={'Host': parsed.hostname}, extensions={'sni_hostname': parsed.hostname}) as response:
                if response.is_redirect:
                    location = response.headers.get('location')
                    if not location: raise ValueError('重定向缺少地址')
                    url = str(httpx.URL(url).join(location)); continue
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2*1024*1024: raise ValueError('响应超过2MiB限制')
                    chunks.append(chunk)
                body = b''.join(chunks)
                # Bytes are already decompressed; Scrapy must not decode them again.
                headers = [(k, v) for k, v in response.headers.multi_items()
                    if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')]
                return httpx.Response(response.status_code, headers=headers, content=body, request=httpx.Request('GET', url))
    raise ValueError('重定向超过限制')


async def fetch_public(url):
    response = await fetch_public_response(url)
    response.raise_for_status()
    content_type = response.headers.get('content-type', '')
    if 'html' in content_type:
        selector = Selector(text=response.text)
        content = '\n'.join(selector.xpath('//body//text()[not(ancestor::script or ancestor::style or ancestor::nav)]').getall())
    elif any(t in content_type for t in ('text/', 'xml', 'json')):
        content = response.text
    else: raise ValueError('该来源不是可读取的文本正文')
    return {'url': str(response.url), 'title': '', 'content': content, 'fetched_at': datetime.now(UTC).isoformat()}


class Search(BaseModel):
    query: str = Field(min_length=1, max_length=300)

class LocalSearch(Search):
    keywords: list[str] = Field(default_factory=list, max_length=6)


class Read(BaseModel):
    id: int

class ReadWeb(BaseModel):
    url: str = Field(max_length=2000)

TOOLS = {
    'search_news': (LocalSearch, '按关键词检索站内新闻'),
    'read_news': (Read, '读取站内新闻原文'),
    'search_packages': (LocalSearch, '按关键词和标签检索其他包，AI总结仅作线索'),
    'read_package': (Read, '读取包及关联原文ID，核验应读取原文'),
    'search_events': (LocalSearch, '检索历史事件及时间线，仅作线索'),
    'read_event': (Read, '读取事件、成员包和时间线，原文仍须另行读取'),
    'search_calendar': (LocalSearch, '检索已有经济日历'),
    'search_web': (Search, '通过SearXNG搜索外部来源，摘要仅作线索'),
    'read_web': (ReadWeb, '读取已发现或已知URL的公开网页正文，链接本身不是证据'),
}


class Research:
    def __init__(self, package=None, event=None):
        self.counts = {'search': 0, 'read': 0, 'search_web': 0, 'read_web': 0}
        self.records = []
        self.sources = {}
        self.cache = {}
        self.urls = set()
        self.package_ids = set()
        self.read_package_ids = set()
        self.read_event_ids = set()
        self.read_event_versions = {}
        self.package = package or {}
        self.event = event or {}
        self.provided_news = {}
        self.provided_packages = {}
        self.web_unavailable = None

    @staticmethod
    def tool_specs():
        return [llm.ToolSpec(n, description, model.model_json_schema()) for n, (model, description) in TOOLS.items()]

    def fits_messages(self, provider, system, messages, schema):
        return fits_context(provider, system, {'messages': messages, 'tools': [
            {'name': t.name, 'description': t.description, 'parameters': t.parameters} for t in self.tool_specs()]}, schema)

    def fits(self, provider, system, payload, schema):
        return self.fits_messages(provider, system, [{'role': 'user', 'text': json.dumps(payload, ensure_ascii=False)}], schema)

    def restore(self, records):
        for record in records:
            name, args, result = record['tool'], record['arguments'], record['result']
            if isinstance(result, dict) and 'error' in result: continue
            # Membership and revisions can change between attempts; reread local objects.
            if name in ('read_event', 'read_package'): continue
            self.cache[name+json.dumps(args, sort_keys=True)] = result
            if name in ('read_web', 'read_news'):
                self.sources[result['article_id']] = result
            if name == 'search_web': self.urls.update(r['url'] for r in result['results'] if r.get('url'))
            if name == 'search_calendar': self.urls.update(r['release_url'] for r in result if r.get('release_url'))
            if name == 'search_packages': self.package_ids.update(r['id'] for r in result)

    async def tool(self, name, arguments):
        if name not in TOOLS: raise ValueError('未知研究工具')
        from alphapulse.news_risk.progress import step
        step('查询资料', tool=name)
        args = TOOLS[name][0].model_validate(arguments)
        if name == 'read_event' and args.id == self.event.get('id'):
            return {**self.event, 'status':'already_provided', 'scope':'ai_summary_not_evidence'}
        if name == 'read_package' and args.id in self.provided_packages:
            return {**self.provided_packages[args.id], 'status':'already_provided', 'scope':'ai_summary_not_evidence'}
        if name == 'read_package' and args.id == self.package.get('id'):
            return {'id': args.id, 'revision': self.package.get('revision'), 'status': 'already_provided',
                'scope': 'ai_summary_not_evidence', 'instruction': '当前包及previous已在输入中；纠错使用给定articles中的id或package.sources中的analysis_id。'}
        if name == 'read_news' and args.id in self.provided_news:
            return {'article_id': self.provided_news[args.id], 'news_id': args.id, 'status': 'already_provided',
                'instruction': '原文已在本轮输入或工具响应中，使用该article_id引用，不重复计为独立来源。'}
        if name == 'search_web' and self.web_unavailable is not None:
            return self.web_unavailable
        key = name+json.dumps(arguments, sort_keys=True)
        if key in self.cache:
            result = self.cache[key]
            # Restored text becomes provided only when returned to this conversation.
            if name == 'read_news': self.provided_news[args.id] = result['article_id']
            return result
        budget = name if name in ('search_web', 'read_web') else 'search' if name.startswith('search') else 'read'
        if self.counts[budget] >= {'search': 4, 'read': 8, 'search_web': 2, 'read_web': 4}[budget]:
            raise ValueError('工具预算已用尽，请明确证据不足')
        self.counts[budget] += 1
        async with SessionLocal() as session:
            if name == 'search_web':
                from alphapulse.core.config import get_settings
                async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                    response = await client.get(get_settings().searxng_url+'/search', params={'q': args.query, 'format': 'json'})
                    response.raise_for_status(); data = response.json()
                rows = data.get('results', [])[:10]
                self.urls.update(r['url'] for r in rows if r.get('url'))
                result = {'results': rows, 'errors': data.get('unresponsive_engines', []), 'scope': 'search_snippets_only', 'status': 'matched' if rows else 'unavailable' if data.get('unresponsive_engines') else 'no_matches'}
                if result['status'] == 'unavailable': self.web_unavailable = result
            elif name == 'read_web':
                result = await fetch_public(args.url)
                result['discovery_method'] = 'discovered' if args.url in self.urls else 'direct'
                identifier = min([0, *self.sources])-1
                self.sources[identifier] = result
                result = {**result, 'article_id': identifier}
            elif name in ('search_events', 'read_event'):
                from alphapulse.models.risk import NewsEvent, EventMember
                if name == 'search_events':
                    rows = (await session.scalars(select(NewsEvent).where(or_(*(NewsEvent.title.icontains(term, autoescape=True) for term in (args.keywords or args.query.split())[:6])), NewsEvent.state != 'retired', NewsEvent.id != self.event.get('id', 0)).limit(20))).all()
                    result = [{'id': e.id, 'title': e.title} for e in rows]
                else:
                    row = await session.get(NewsEvent, args.id)
                    if row is None: raise ValueError('事件不存在')
                    self.read_event_ids.add(row.id); self.read_event_versions[row.id]=row.revision
                    members = list((await session.scalars(select(EventMember.package_id).where(EventMember.event_id == row.id))).all())
                    packages = list(await session.scalars(select(NewsSummary).where(NewsSummary.id.in_(members),NewsSummary.replacement_ids==[],NewsSummary.state!='withdrawn')))
                    result = {'packages':[{'id':p.id,'revision':p.revision,'fact':p.fact,'tags':p.tags} for p in packages], 'id':row.id, 'revision':row.revision, 'title':row.title, 'fact':row.fact, 'result':row.result, 'package_ids':members, 'scope':'ai_summary_not_evidence'}
            elif name in ('search_news', 'search_packages'):
                model = NewsItem if name == 'search_news' else NewsSummary
                from alphapulse.news_risk.retrieval import terms_for, matching
                conditions, score = matching(model, terms_for(args.query, args.keywords))
                if model is NewsSummary: conditions = conditions & (NewsSummary.replacement_ids == []) & (NewsSummary.state != 'withdrawn') & (NewsSummary.id != self.package.get('id', 0)) & NewsSummary.id.not_in(self.provided_packages)
                rows = (await session.scalars(select(model).where(conditions).order_by(score.desc(), model.published_at.desc(), model.id).limit(20))).all()
                result = [{'id': r.id, 'title': r.title} for r in rows]
                if model is NewsSummary: self.package_ids.update(r.id for r in rows)
            elif name == 'read_news':
                row = await session.get(NewsItem, args.id)
                if row is None: raise ValueError('新闻不存在')
                identifier = min([0, *self.sources])-1
                result = {'article_id': identifier, 'news_id': row.id, 'title': row.title, 'content': row.content_text,
                    'summary': row.summary, 'url': row.url, 'published_at': row.published_at.isoformat()}
                self.sources[identifier] = result
                self.provided_news[row.id] = identifier
            elif name == 'read_package':
                from alphapulse.models.risk import SummaryArticle, ArticleAnalysis, EventMember
                row = await session.get(NewsSummary, args.id)
                if row is None: raise ValueError('包不存在')
                self.package_ids.add(row.id); self.read_package_ids.add(row.id)
                ids = (await session.scalars(select(ArticleAnalysis.news_id).join(SummaryArticle).join(NewsItem).where(SummaryArticle.summary_id == row.id, SummaryArticle.active.is_(True), ArticleAnalysis.content_hash == NewsItem.content_hash).distinct())).all()
                assignments = (await session.scalars(select(ArticleAnalysis).join(SummaryArticle).join(NewsItem).where(SummaryArticle.summary_id == row.id, SummaryArticle.active.is_(True), ArticleAnalysis.content_hash == NewsItem.content_hash))).all()
                membership = await session.get(EventMember, row.id)
                result = {'main_event_id': membership.event_id if membership else None, 'replacement_ids': row.replacement_ids, 'state': row.state, 'id': row.id, 'revision': row.revision, 'fact': row.fact, 'result': row.result, 'articles': [{'analysis_id': a.id, **a.input} for a in assignments], 'news_ids': ids, 'scope': 'ai_summary_not_evidence'}
            else:
                from alphapulse.models.calendar import EconomicRelease
                rows = (await session.scalars(select(EconomicRelease).where(or_(*(EconomicRelease.title.icontains(term, autoescape=True) for term in (args.keywords or args.query.split())[:6]))).limit(20))).all()
                result = [{'id': r.id, 'title': r.title, 'values': r.values, 'release_url': r.release_url, 'scheduled_at': r.scheduled_at.isoformat()} for r in rows]
                self.urls.update(r.release_url for r in rows if r.release_url)
        self.cache[key] = result
        self.records.append({'tool': name, 'arguments': arguments, 'result': result})
        return result

    async def run(self, provider, system, payload, schema):
        # Only full articles actually present in the prompt can replace a source read.
        self.provided_news.update({a['news_id']: a['id'] for a in payload.get('articles', []) if a.get('news_id') is not None})
        self.sources.update({a['id']: a for a in payload.get('articles', [])})
        self.provided_packages.update({p['id']:p for p in payload.get('packages',[])})
        specs = self.tool_specs()
        submit = llm.ToolSpec('submit', '提交完整研判，引用实际读取来源', schema.model_json_schema())
        messages = [{'role': 'user', 'text': json.dumps(payload, ensure_ascii=False)}]
        usages = self.usages = []
        from alphapulse.news_risk.progress import step, current
        for round_no in range(9):
            if current.get() is not None: step('模型研判', round=round_no+1, model=provider.model, reasoning_effort=provider.reasoning_effort)
            if not self.fits_messages(provider, system, messages, schema):
                raise ValueError('研究上下文不足，请增大上下文配置；证据未截断')
            completion = None
            async for item in model_stream(provider, system, messages, [*specs, submit] if round_no < 8 else [submit], provider.reasoning_effort):
                if isinstance(item, llm.Completion): completion = item
            if completion is not None: usages.append(completion.usage)
            if completion is None or completion.truncated: raise ValueError('研究输出未完成或被截断')
            calls = completion.tool_calls
            if any(c['name'] == 'submit' for c in calls):
                if len(calls) != 1: raise ValueError('提交结果不能同时执行查询')
                result = await validate_submission(provider, system, messages, schema, completion, usages)
                return result, {'rounds': usages, 'tools': self.records}
            if round_no == 8 or not calls: raise ValueError('研究模型未提交结果')
            assistant = {'role': 'assistant', 'text': completion.text, 'tool_calls': calls}
            if completion.thinking_blocks: assistant['thinking_blocks'] = completion.thinking_blocks
            messages.append(assistant)
            for call in calls:
                try: result = await self.tool(call['name'], call['arguments'])
                except Exception as error:
                    result = {'error': str(error)}
                    self.records.append({'tool': call['name'], 'arguments': call['arguments'], 'result': result})
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'result': json.dumps(result, ensure_ascii=False, default=str)})
        raise ValueError('研究轮次已用尽')
