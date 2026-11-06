"""隔离 Redis 验证实际 Lua 原子操作；不访问生产接入。"""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

import httpx
import openai
import pytest



def test_global_failure_takes_over_local_retry_and_recovers(trading, monkeypatch):
    from alphapulse.ai import llm
    states = iter([
        dict(level=0, epoch=0, admitted=True, probe=False, now=0, retry_at=0),
        dict(level=1, epoch=1, admitted=False, probe=False, now=0, retry_at=180),
        dict(level=1, epoch=1, admitted=False, probe=False, now=0, retry_at=180),
        dict(level=1, epoch=1, admitted=True, probe=True, now=180, retry_at=180),
    ])
    async def coordinate(operation, *args):
        if operation in ('enter', 'failure'):
            return next(states)
        return dict(level=0)
    gate = N(call=AsyncMock(side_effect=coordinate))
    monkeypatch.setattr(llm, 'recovery_gate', lambda _: gate)
    monkeypatch.setattr(llm.asyncio, 'sleep', AsyncMock())
    local = AsyncMock()
    monkeypatch.setattr(llm, '_wait_local', local)
    calls = []
    async def adapter(*args):
        calls.append(args)
        if len(calls) == 1:
            raise openai.RateLimitError('limited', response=httpx.Response(429, request=httpx.Request('POST', 'https://test.invalid')), body={})
        yield llm.Completion(text='done')
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    async def run():
        return [x async for x in llm.stream(N(protocol='openai'), '', [], [])]
    result = asyncio.run(run())
    assert result[-1].text == 'done' and len(calls) == 2
    assert any(isinstance(x, llm.RetryStatus) and x.scope == 'upstream' for x in result)
    local.assert_not_awaited()


def test_coordinator_failure_does_not_send(trading, monkeypatch):
    from alphapulse.ai import llm
    adapter = AsyncMock()
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, 'recovery_gate', lambda _: N(call=AsyncMock(side_effect=RuntimeError('offline'))))
    async def run():
        with pytest.raises(llm.LlmError, match='协调不可用'):
            async for _ in llm.stream(N(protocol='openai'), '', [], []):
                pass
    asyncio.run(run())
    adapter.assert_not_called()


def test_fail_fast_does_not_send_or_wait(trading, monkeypatch):
    from alphapulse.ai import llm
    gate = N(call=AsyncMock(return_value=dict(level=2, epoch=1, admitted=False, probe=False, now=100, retry_at=700)))
    monkeypatch.setattr(llm, 'recovery_gate', lambda _: gate)
    adapter = AsyncMock()
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    async def run():
        token = llm.fail_fast.set(True)
        try:
            with pytest.raises(llm.LlmError, match='下次尝试'):
                async for _ in llm.stream(N(protocol='openai'), '', [], []):
                    pass
        finally:
            llm.fail_fast.reset(token)
    asyncio.run(run())
    adapter.assert_not_called()


def test_completed_probe_is_confirmed_before_caller_returns(trading, monkeypatch):
    from alphapulse.ai import llm
    gate = N(call=AsyncMock(return_value=dict(level=0, epoch=1, admitted=True, probe=True, now=1, retry_at=0)))
    monkeypatch.setattr(llm, 'recovery_gate', lambda _: gate)
    async def adapter(*args):
        yield llm.Completion(text='done')
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    async def run():
        stream = llm.stream(N(protocol='openai'), '', [], [])
        assert (await anext(stream)).text == 'done'
        assert any(call.args[0] == 'success' for call in gate.call.await_args_list)
        await stream.aclose()
    asyncio.run(run())


def test_retry_refreshes_only_current_request(trading, monkeypatch):
    from alphapulse.ai import llm
    calls = []
    async def adapter(provider, system, messages, tools, effort):
        calls.append(list(messages))
        if len(calls) == 1:
            raise httpx.ConnectError('unavailable')
        yield llm.Completion(text='ok')
    messages = [{'role': 'tool', 'tool_call_id': 'already-done', 'result': 'retained'}]
    async def refresh():
        messages.append({'role': 'user', 'text': 'fresh snapshot'})
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, '_wait_local', AsyncMock())
    async def run():
        return [x async for x in llm.stream(N(protocol='openai'), '', messages, [], before_retry=refresh)]
    assert asyncio.run(run())[-1].text == 'ok'
    assert len(calls) == 2 and calls[1][0] == calls[0][0]
    assert calls[1][-1]['text'] == 'fresh snapshot'


@pytest.mark.parametrize('failed_operation', ['success', 'release', 'both'])
def test_completed_result_survives_coordination_write_failure(trading, monkeypatch, failed_operation):
    from alphapulse.ai import llm
    from alphapulse.news_research.model import complete
    from alphapulse.news_research.contracts import IndexResult
    calls = []

    async def coordinate(operation, *args):
        calls.append(operation)
        if operation == failed_operation or failed_operation == 'both' and operation in ('success', 'release'):
            raise RuntimeError('synthetic Redis outage')
        return dict(level=0, epoch=0, admitted=True, probe=False, now=0, retry_at=0)

    async def adapter(*args):
        yield llm.Completion(tool_calls=[{'name': 'submit', 'id': '1', 'arguments': {
            'keywords': [{'canonical': '新闻', 'aliases': ['news'], 'quote': 'news'}],
        }}], usage={'input_tokens': 100, 'output_tokens': 10})

    monkeypatch.setattr(llm, 'recovery_gate', lambda _: N(call=coordinate))
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    async def run():
        return await complete(N(protocol='openai', max_tokens=100, context_window=250000,
                                reasoning_effort=None), '', {}, IndexResult)
    output, usage = asyncio.run(run())
    assert output.keywords[0].canonical == '新闻'
    assert usage == {'input_tokens': 100, 'output_tokens': 10}
    assert calls.count('enter') == 1 and 'release' in calls


def test_cancellation_survives_lease_release_failure(trading, monkeypatch):
    from alphapulse.ai import llm
    started = asyncio.Event()
    async def coordinate(operation, *args):
        if operation == 'release':
            raise RuntimeError('synthetic Redis outage')
        return dict(level=0, epoch=0, admitted=True, probe=False, now=0, retry_at=0)
    async def adapter(*args):
        started.set()
        await asyncio.Event().wait()
        yield
    monkeypatch.setattr(llm, 'recovery_gate', lambda _: N(call=coordinate))
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    async def run():
        async def consume():
            return [x async for x in llm.stream(N(protocol='openai'), '', [], [])]
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
