"""Research and briefing acceptance uses synthetic I/O only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def test_private_and_mixed_dns_are_rejected(trading, monkeypatch):
    from alphapulse.core.public_http import public_address
    async def check():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[(None, None, None, None, ('127.0.0.1', 80))]))
        with pytest.raises(ValueError, match='非公开'): await public_address('https://example.invalid/test')
        monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[(None, None, None, None, ('8.8.8.8', 80)), (None, None, None, None, ('10.0.0.1', 80))]))
        with pytest.raises(ValueError, match='非公开'): await public_address('https://example.invalid/test')
        for url in ('file:///etc/passwd', 'http://user:password@example.invalid', 'http://example.invalid:8000'):
            with pytest.raises(ValueError): await public_address(url)
    asyncio.run(check())




def test_high_importance_positive_package_is_valid(trading):
    from alphapulse.news_research import pipeline
    from test_news_pipeline import event_result
    result = event_result(pipeline, [pipeline.Evidence(article_id=1, quote='突破')])
    result.risk_stars = 0; result.importance_stars = 5
    pipeline.validate_event(result, {1: {'title': '科技突破'}})
    assert result.importance_stars == 5 and result.risk_stars == 0


def test_public_fetch_pins_address_and_rechecks_redirect(trading, monkeypatch):
    import httpx
    from alphapulse.core import public_http as research
    calls = []
    original = httpx.AsyncClient
    async def resolve(url):
        from urllib.parse import urlsplit
        if 'private.invalid' in url: raise ValueError('非公开地址')
        return urlsplit(url), '8.8.8.8'
    async def handle(request):
        calls.append(request)
        assert request.url.host == '8.8.8.8' and request.headers['host'] == 'public.invalid'
        assert request.extensions['sni_hostname'] == 'public.invalid'
        return httpx.Response(302, headers={'location': 'http://private.invalid'})
    monkeypatch.setattr(research, 'public_address', resolve)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    with pytest.raises(ValueError, match='非公开'):
        asyncio.run(research.fetch_public_response('https://public.invalid'))
    assert len(calls) == 1




def test_restored_news_is_sent_before_reusing_its_reference(trading, monkeypatch):
    import json
    from pydantic import BaseModel
    from alphapulse.news_research import tools as research
    class Result(BaseModel):
        summary: str
    review = research.Research({'id': 42})
    review.restore([{'tool': 'read_news', 'arguments': {'id': 123},
        'result': {'article_id': -1, 'news_id': 123, 'content': 'Restored original'}}])
    rounds = []
    async def stream(provider, system, messages, tools, effort):
        rounds.append(len(messages))
        if len(rounds) == 2:
            assert json.loads(messages[-1]['result'])['content'] == 'Restored original'
        if len(rounds) == 3:
            reply = json.loads(messages[-1]['result'])
            assert reply['article_id'] == -1 and reply['status'] == 'already_provided'
            assert 'content' not in reply
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit',
                'arguments': {'summary': 'Restored original'}}])
        else:
            yield research.llm.Completion(tool_calls=[{'id': str(len(rounds)), 'name': 'read_news', 'arguments': {'id': 123}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    result, _ = asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None),
        'rules', {'articles': []}, Result))
    assert result.summary == 'Restored original' and len(rounds) == 3
    assert review.counts['read'] == 0 and not review.records


def test_current_input_takes_precedence_over_restored_news(trading, monkeypatch):
    from pydantic import BaseModel
    from alphapulse.news_research import tools as research
    class Result(BaseModel):
        summary: str
    review = research.Research()
    review.restore([{'tool': 'read_news', 'arguments': {'id': 123},
        'result': {'article_id': -1, 'news_id': 123, 'content': 'Old original'}}])
    async def stream(provider, system, messages, tools, effort):
        if len(messages) == 1:
            yield research.llm.Completion(tool_calls=[{'id': 'read', 'name': 'read_news', 'arguments': {'id': 123}}])
        else:
            import json
            reply = json.loads(messages[-1]['result'])
            assert reply['article_id'] == 9 and reply['status'] == 'already_provided'
            assert 'Old original' not in json.dumps(messages)
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit', 'arguments': {'summary': 'Current original'}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None), 'rules',
        {'articles': [{'id': 9, 'news_id': 123, 'content': 'Current original'}]}, Result))
    assert review.counts['read'] == 0


def test_research_stops_after_eight_tool_rounds_and_retains_failures(trading, monkeypatch):
    from alphapulse.news_research import tools as research
    from pydantic import BaseModel
    class Result(BaseModel):
        summary: str
    rounds = []
    async def stream(provider, system, messages, tools, effort):
        rounds.append(tools)
        if len(rounds) == 9:
            assert [tool.name for tool in tools] == ['submit']
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit', 'arguments': {'summary': '证据不足'}}])
        else:
            yield research.llm.Completion(tool_calls=[{'id': str(len(rounds)), 'name': 'unknown_tool', 'arguments': {}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    review = research.Research()
    result, _ = asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None), 'rules', {}, Result))
    assert result.summary == '证据不足'
    assert len(rounds) == 9 and len(review.records) == 8
    assert all('error' in record['result'] for record in review.records)


def test_provided_original_is_reused_without_io(trading, monkeypatch):
    from alphapulse.news_research import tools as research
    from pydantic import BaseModel
    class Result(BaseModel):
        summary: str
    async def stream(provider, system, messages, tools, effort):
        if len(messages) == 1:
            yield research.llm.Completion(tool_calls=[{'id': 'read', 'name': 'read_news', 'arguments': {'id': 123}}])
        else:
            import json
            response = json.loads(messages[-1]['result'])
            assert response['article_id'] == 9 and 'content' not in response
            yield research.llm.Completion(tool_calls=[{'id': 'submit', 'name': 'submit', 'arguments': {'summary': 'Done'}}])
    monkeypatch.setattr(research.llm, 'stream', stream)
    review = research.Research()
    asyncio.run(review.run(SimpleNamespace(max_tokens=1000, context_window=200000, reasoning_effort=None), '',
        {'articles': [{'id': 9, 'news_id': 123, 'content': 'Original'}]}, Result))
    assert review.sources[9]['content'] == 'Original'
    assert review.counts['read'] == 0 and not review.records






@pytest.mark.parametrize('tool,args', [('search_web', {'query': 'test'}), ('read_web', {'url': 'https://example.invalid'})])
def test_external_tools_are_removed_and_cannot_restore_sources(trading, tool, args):
    from alphapulse.news_research.service import Research
    reader = Research(event={'id': 1})
    assert tool not in [spec.name for spec in reader.tool_specs()]
    reader.restore([{'tool': tool, 'arguments': args, 'result': {'article_id': -1, 'content': 'Old external body'}}])
    assert not reader.records and not reader.sources and not reader.cache
    with pytest.raises(ValueError, match='未知'):
        asyncio.run(reader.tool(tool, args))
