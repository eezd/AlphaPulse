"""Read-only news and external research tools with bounded calls."""
import json
from sqlalchemy import select, or_
from pydantic import BaseModel, Field
from alphapulse.ai import llm
from alphapulse.core.db import SessionLocal
from alphapulse.models import NewsItem
from alphapulse.news_research.model import fits_context, validate_submission, model_stream


class Search(BaseModel):
    query: str = Field(min_length=1, max_length=300)

class LocalSearch(Search):
    keywords: list[str] = Field(default_factory=list, max_length=6)


class Read(BaseModel):
    id: int

TOOLS = {
    'search_news': (LocalSearch, '按关键词检索站内新闻'),
    'read_news': (Read, '读取站内新闻原文'),
    'search_packages': (LocalSearch, '按关键词和标签检索其他包，AI总结仅作线索'),
    'read_package': (Read, '读取包及关联原文ID，核验应读取原文'),
    'search_events': (LocalSearch, '检索历史事件及时间线，仅作线索'),
    'read_event': (Read, '读取事件、成员包和时间线，原文仍须另行读取'),
    'search_calendar': (LocalSearch, '检索已有经济日历'),
}


class Research:
    def __init__(self, event=None):
        self.counts = {'search': 0, 'read': 0}
        self.records = []
        self.sources = {}
        self.cache = {}
        self.package_ids = set()
        self.read_package_ids = set()
        self.read_event_ids = set()
        self.read_event_versions = {}
        self.event = event or {}
        self.provided_news = {}
        self.provided_packages = {}

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
            if name not in TOOLS: continue
            if isinstance(result, dict) and 'error' in result: continue
            # Membership and revisions can change between attempts; reread local objects.
            if name in ('read_event', 'read_package'): continue
            self.cache[name+json.dumps(args, sort_keys=True)] = result
            if name == 'read_news':
                self.sources[result['article_id']] = result
            if name == 'search_packages': self.package_ids.update(r['id'] for r in result)

    async def tool(self, name, arguments):
        if name not in TOOLS: raise ValueError('未知研究工具')
        from alphapulse.news_research.progress import step
        step('查询资料', tool=name)
        args = TOOLS[name][0].model_validate(arguments)
        if name == 'read_event' and args.id == self.event.get('id'):
            return {**self.event, 'status':'already_provided', 'scope':'ai_summary_not_evidence'}
        if name == 'read_package' and args.id in self.provided_packages:
            return {**self.provided_packages[args.id], 'status':'already_provided', 'scope':'ai_summary_not_evidence'}
        if name == 'read_news' and args.id in self.provided_news:
            return {'article_id': self.provided_news[args.id], 'news_id': args.id, 'status': 'already_provided',
                'instruction': '原文已在本轮输入或工具响应中，使用该article_id引用，不重复计为独立来源。'}
        key = name+json.dumps(arguments, sort_keys=True)
        if key in self.cache:
            result = self.cache[key]
            # Restored text becomes provided only when returned to this conversation.
            if name == 'read_news': self.provided_news[args.id] = result['article_id']
            return result
        budget = 'search' if name.startswith('search') else 'read'
        if self.counts[budget] >= {'search': 4, 'read': 8}[budget]:
            raise ValueError('工具预算已用尽，请明确证据不足')
        self.counts[budget] += 1
        async with SessionLocal() as session:
            if name == 'search_news':
                from alphapulse.news_research.retrieval import terms_for, matching
                conditions, score = matching(NewsItem, terms_for(args.query, args.keywords))
                rows = await session.scalars(select(NewsItem).where(conditions).order_by(
                    score.desc(), NewsItem.published_at.desc(), NewsItem.id).limit(20))
                result = [{'id': row.id, 'title': row.title} for row in rows]
            elif name == 'read_news':
                row = await session.get(NewsItem, args.id)
                if row is None: raise ValueError('新闻不存在')
                identifier = min([0, *self.sources])-1
                result = {'article_id': identifier, 'news_id': row.id, 'title': row.title, 'content': row.content_text,
                    'summary': row.summary, 'url': row.url, 'published_at': row.published_at.isoformat()}
                self.sources[identifier] = result
                self.provided_news[row.id] = identifier
            else:
                from alphapulse.models.calendar import EconomicRelease
                rows = (await session.scalars(select(EconomicRelease).where(or_(*(EconomicRelease.title.icontains(term, autoescape=True) for term in (args.keywords or args.query.split())[:6]))).limit(20))).all()
                result = [{'id': r.id, 'title': r.title, 'values': r.values, 'release_url': r.release_url, 'scheduled_at': r.scheduled_at.isoformat()} for r in rows]
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
        from alphapulse.news_research.progress import step, current
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
