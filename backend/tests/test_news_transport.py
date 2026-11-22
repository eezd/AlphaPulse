"""News retry budgets use synthetic providers only."""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
import httpx
import openai
import pytest


@pytest.mark.parametrize('mode', ['success', 'partial', 'thinking', 'timeout', 'auth', 'parameter', 'long_wait', 'tool_fragment'])
def test_news_transport_retry_budget_and_output_guard(app_env, monkeypatch, mode):
    from alphapulse.ai import llm
    from alphapulse.ai.submission import model_stream
    from alphapulse.ai.progress import current
    calls = []
    async def adapter(*args):
        calls.append(args)
        if mode == 'partial': yield llm.TextDelta('started')
        if mode == 'thinking': yield llm.ThinkingStarted()
        if mode == 'tool_fragment': llm._news_output()
        if mode in ('success', 'long_wait', 'timeout') and len(calls) == 3:
            yield llm.Completion(usage={'input_tokens': 1})
            return
        request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
        if mode in ('timeout', 'partial', 'thinking', 'tool_fragment'):
            raise openai.APITimeoutError(request=request)
        if mode == 'auth': raise openai.AuthenticationError('denied', response=httpx.Response(401, request=request), body={})
        if mode == 'parameter': raise openai.BadRequestError('invalid', response=httpx.Response(400, request=request), body={})
        if mode == 'long_wait': raise openai.RateLimitError('wait', response=httpx.Response(429, headers={'retry-after': '18000'}, request=request), body={})
        raise openai.APIError('Concurrency limit exceeded for account', request, body={'error': {'message': 'Concurrency limit exceeded for account'}})
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    sleep = AsyncMock()
    monkeypatch.setattr(llm, '_wait_local', sleep)
    record = {}
    async def run():
        token = current.set(record)
        try: return [item async for item in model_stream(N(protocol='openai'), '', [], [], 'max')]
        finally: current.reset(token)
    retries = mode in ('success', 'long_wait', 'timeout')
    if retries: assert asyncio.run(run())[-1].usage == {'input_tokens': 1}
    else:
        with pytest.raises(llm.LlmError): asyncio.run(run())
    assert len(calls) == (3 if retries else 1)
    assert [call.args[0] for call in sleep.await_args_list] == ([5, 5] if retries else [])
    assert llm.news_retry.get() is None
    if retries:
        assert record['retry_attempt'] == 2 and record['retry_scope'] is None


def test_cancelling_backoff_releases_context_and_progress(app_env, monkeypatch):
    from alphapulse.ai import llm
    from alphapulse.ai.submission import model_stream
    from alphapulse.ai import progress
    entered = asyncio.Event()
    async def adapter(*args):
        raise openai.APIError('Concurrency limit exceeded', httpx.Request('POST', 'https://example.invalid'), body={})
        yield
    async def sleep(delay, coordinate, *, started_at):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, '_wait_local', sleep)
    async def run():
        async def consume():
            try:
                async for _ in model_stream(N(protocol='openai'), '', [], [], None): pass
            finally: assert llm.news_retry.get() is None
        task=asyncio.create_task(progress.execute(('events', 4), consume()))
        await entered.wait()
        assert '重试' in next(iter(progress.active.values()))['phase']
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not progress.active
    asyncio.run(run())


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('news', [True, False])
def test_sdk_retry_is_disabled_for_every_request(app_env, monkeypatch, protocol, news):
    from alphapulse.ai import llm
    options=[]
    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.chat=N(completions=N(create=self.create))
            self.messages=N(create=self.create)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            async def events():
                if protocol == 'openai':
                    yield N(usage=None, choices=[N(finish_reason='stop', delta=N(content=None,tool_calls=[]))])
                else: yield N(type='message_stop')
            return events()
    monkeypatch.setattr(llm, 'decrypt', lambda _: 'synthetic')
    monkeypatch.setattr(llm.openai if protocol == 'openai' else llm.anthropic,
                        'AsyncOpenAI' if protocol == 'openai' else 'AsyncAnthropic', Client)
    provider=N(protocol=protocol,api_key_encrypted='test',base_url='https://example.invalid',model='test',max_tokens=10)
    async def run():
        token=llm.news_retry.set({'output':False} if news else None)
        try: return [item async for item in llm.stream(provider,'rules',[],[],None)]
        finally: llm.news_retry.reset(token)
    assert asyncio.run(run())[-1] == llm.Completion()
    assert options[0]['max_retries'] == 0
    assert options[0]['timeout'] == 600


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('output', ['text', 'thinking', 'tool_fragment'])
@pytest.mark.parametrize('failure', ['timeout', 'disconnect'])
def test_real_adapter_partial_output_is_never_replayed(app_env, monkeypatch, protocol, output, failure):
    from alphapulse.ai import llm
    options = []
    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.chat = N(completions=N(create=self.create))
            self.messages = N(create=self.create)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            async def chunks():
                if protocol == 'openai':
                    delta = N(content='started' if output == 'text' else None,
                              reasoning_content='reasoning' if output == 'thinking' else None,
                              tool_calls=[N(index=0, id='call', function=N(name='search', arguments='{'))]
                              if output == 'tool_fragment' else [])
                    yield N(usage=None, choices=[N(finish_reason=None, delta=delta)])
                elif output == 'text':
                    yield N(type='content_block_delta', index=0, delta=N(type='text_delta', text='started'))
                elif output == 'thinking':
                    yield N(type='content_block_start', index=0,
                            content_block=N(type='thinking', thinking='', signature=''))
                else:
                    yield N(type='content_block_start', index=0,
                            content_block=N(type='tool_use', id='call', name='search'))
                    yield N(type='content_block_delta', index=0,
                            delta=N(type='input_json_delta', partial_json='{'))
                transport = llm.httpx if protocol == 'openai' else llm.httpx2
                if failure == 'timeout': raise transport.ReadTimeout('timeout after output')
                raise transport.ReadError('disconnected after output')
            return chunks()
    monkeypatch.setattr(llm, 'decrypt', lambda _: 'synthetic')
    monkeypatch.setattr(llm.openai if protocol == 'openai' else llm.anthropic,
                        'AsyncOpenAI' if protocol == 'openai' else 'AsyncAnthropic', Client)
    wait = AsyncMock()
    monkeypatch.setattr(llm, '_wait_local', wait)
    provider = N(protocol=protocol, api_key_encrypted='test', base_url='https://example.invalid',
                 model='test', max_tokens=10)
    async def run():
        with pytest.raises(llm.LlmError):
            async for _ in llm.stream(provider, '', [], []):
                assert llm.news_retry.get() is None
        assert llm.news_retry.get() is None
    asyncio.run(run())
    assert len(options) == 1
    wait.assert_not_awaited()
