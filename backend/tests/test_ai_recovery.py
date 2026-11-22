"""请求独立重试与展示状态；仅合成适配器、受控时间和内存 Redis 边界。"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

import httpx
import pytest
from redis.exceptions import RedisError


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('failure', ['timeout', '429', '503', 'transport', 'stream', 'interrupted', 'unfinished'])
def test_independent_retry_sequence_until_success(app_env, monkeypatch, protocol, failure):
    from alphapulse.ai import llm, recovery
    sdk = llm.openai if protocol == 'openai' else llm.anthropic
    request = httpx.Request('POST', 'https://example.invalid')
    calls = []

    async def adapter(*args):
        calls.append(args)
        if len(calls) == 10:
            yield llm.Completion(text='done', usage={'input_tokens': 7})
            return
        if failure == 'unfinished':
            return
        if failure == 'timeout':
            raise sdk.APITimeoutError(request=request)
        if failure in ('429', '503'):
            raise sdk.APIStatusError('temporary', response=httpx.Response(
                int(failure), headers={'retry-after': '18000'}, request=request), body={})
        if failure in ('stream', 'interrupted'):
            message = 'overloaded' if failure == 'stream' else 'Upstream response stream was interrupted'
            if protocol == 'openai':
                raise sdk.APIError(message, request, body={'error': {'message': message}})
            raise sdk.APIStatusError(message, response=httpx.Response(200, request=request), body={})
        raise httpx.ConnectError('offline')

    monkeypatch.setattr(llm, '_stream_' + protocol, adapter)
    wait = AsyncMock()
    refresh = AsyncMock()
    monkeypatch.setattr(llm, '_wait_local', wait)

    async def run():
        return [event async for event in llm.stream(N(protocol=protocol), '', [], [], before_retry=refresh)]

    result = asyncio.run(run())
    assert len(calls) == 10 and result[-1].usage == {'input_tokens': 7}
    assert [call.args[0] for call in wait.await_args_list] == [5, 5, 5, 5, 5, 180, 600, 900, 900]
    statuses = result[:-1]
    assert [item.attempt for item in statuses] == list(range(1, 10))
    assert all(item.scope == 'request' for item in statuses)
    assert [call.args[0].level for call in recovery.mark_waiting.await_args_list] == [0]*5 + [1, 2, 3, 3]
    assert recovery.clear_waiting.await_args_list == recovery.mark_waiting.await_args_list
    assert refresh.await_count == 9 and llm.news_retry.get() is None


def test_interrupted_stream_after_output_is_not_replayed(app_env, monkeypatch):
    from alphapulse.ai import llm

    for event_type in ('text', 'thinking', 'raw'):
        calls = []
        wait = AsyncMock()

        async def adapter(*args, event_type=event_type, calls=calls):
            calls.append(args)
            if event_type == 'raw':
                llm._news_output()
            elif event_type == 'thinking':
                yield llm.ThinkingStarted()
            else:
                yield llm.TextDelta('partial')
            raise llm.openai.APIError('Upstream response stream was interrupted',
                                      httpx.Request('POST', 'https://example.invalid'), body={})

        monkeypatch.setattr(llm, '_stream_openai', adapter)
        monkeypatch.setattr(llm, '_wait_local', wait)

        async def run():
            with pytest.raises(llm.LlmError, match='Upstream response stream was interrupted'):
                async for _ in llm.stream(N(protocol='openai'), '', [], []):
                    pass

        asyncio.run(run())
        assert len(calls) == 1
        wait.assert_not_awaited()


def test_waiting_request_does_not_gate_or_reset_peers(app_env, monkeypatch):
    from alphapulse.ai import llm

    async def run():
        at_long_wait = asyncio.Event()
        release = asyncio.Event()
        attempts, waits = {}, []

        async def adapter(provider, *args):
            attempts[provider.name] = attempts.get(provider.name, 0) + 1
            if provider.name == 'a' and attempts['a'] <= 8 or provider.name == 'b' and attempts['b'] == 1:
                raise httpx.ConnectError('offline')
            yield llm.Completion(text=provider.name)

        async def wait(delay, record, *, started_at):
            waits.append((delay, record))
            if delay == 900:
                at_long_wait.set()
                await release.wait()

        monkeypatch.setattr(llm, '_stream_openai', adapter)
        monkeypatch.setattr(llm, '_wait_local', wait)

        async def consume(name):
            return [x async for x in llm.stream(N(protocol='openai', name=name), '', [], [])]

        first = asyncio.create_task(consume('a'))
        await at_long_wait.wait()
        deadline = waits[-1][1]
        second = await consume('b')
        assert second[-1].text == 'b' and second[0].attempt == 1
        assert not first.done() and attempts == {'a': 8, 'b': 2}
        assert waits[-1][1].request_id != deadline.request_id
        release.set()
        output = await first
        assert output[-1].text == 'a' and output[-2].attempt == 8
        assert waits[-2][1] == deadline
    asyncio.run(run())


@pytest.mark.parametrize('delay', [5, 900])
@pytest.mark.parametrize('cleanup_failure', [False, True, 'stall'])
def test_cancel_wait_cleans_state_and_preserves_cancel(app_env, monkeypatch, delay, cleanup_failure):
    from alphapulse.ai import llm, recovery
    entered = asyncio.Event()
    redis = N(pipeline=lambda **kwargs: (_ for _ in ()).throw(RedisError('offline')))
    if cleanup_failure == 'stall':
        redis = StalledRedis()
        monkeypatch.setattr(recovery, 'DISPLAY_TIMEOUT', 0.01)
    if cleanup_failure:
        monkeypatch.setattr(recovery, 'redis', redis)
        monkeypatch.setattr(recovery, 'clear_waiting', app_env['clear_waiting'])
    count = 0

    async def adapter(*args):
        nonlocal count
        count += 1
        raise httpx.ConnectError('offline')
        yield

    real_wait = llm._wait_local
    async def sleep(seconds):
        entered.set()
        await asyncio.Event().wait()

    async def wait(seconds, record, *, started_at):
        if seconds == delay:
            await real_wait(seconds, record, started_at=started_at)

    monkeypatch.setattr(llm.asyncio, 'sleep', sleep)
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, '_wait_local', wait)

    async def run():
        async def consume():
            return [x async for x in llm.stream(N(protocol='openai'), '', [], [])]
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
    asyncio.run(run())
    assert count == (1 if delay == 5 else 8)
    if cleanup_failure == 'stall':
        assert redis.finished == count
    if not cleanup_failure:
        assert recovery.clear_waiting.await_count == count


def test_retry_refreshes_only_current_request_and_retains_tools(app_env, monkeypatch):
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


def test_before_retry_contract_failure_is_not_retried(app_env, monkeypatch):
    from alphapulse.ai import llm
    calls = []
    async def adapter(*args):
        calls.append(args)
        raise httpx.ConnectError('offline')
        yield
    refresh = AsyncMock(side_effect=llm.LlmError('snapshot invalid'))
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, '_wait_local', AsyncMock())
    async def run():
        with pytest.raises(llm.LlmError, match='snapshot invalid'):
            async for _ in llm.stream(N(protocol='openai'), '', [], [], before_retry=refresh):
                pass
    asyncio.run(run())
    assert len(calls) == 1
    refresh.assert_awaited_once()


def test_wait_uses_monotonic_deadline_and_renews_without_changing_retry_time(app_env, monkeypatch):
    from alphapulse.ai import llm, recovery
    clock, sleeps = [0], []
    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(llm.asyncio, 'get_running_loop', lambda: N(time=lambda: clock[0]))
    monkeypatch.setattr(llm.asyncio, 'sleep', sleep)
    wait = recovery.RetryWait('scope', 'id', 1234, 1, True)
    asyncio.run(llm._wait_local(95, wait, started_at=0))
    assert sleeps == [30, 30, 30, 5] and clock[0] == 95
    assert [call.args[0] for call in recovery.mark_waiting.await_args_list] == [wait]*3


class MemoryRedis:
    """事务边界的最小替身，记录有序集合及过期命令，不打开连接。"""
    def __init__(self):
        self.rows, self.ttls = {}, {}
        self.transactions = []

    def pipeline(self, *, transaction):
        assert transaction
        commands = []
        self.transactions.append(commands)
        store = self
        class Pipeline:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def __getattr__(self, name):
                def queue(*args):
                    commands.append((name, args))
                return queue
            async def execute(self):
                results = []
                for name, args in commands:
                    key, *values = args
                    rows = store.rows.setdefault(key, {})
                    if name == 'zadd': rows.update(values[0])
                    elif name == 'expire': store.ttls[key] = values[0]
                    elif name == 'zrem': rows.pop(values[0], None)
                    elif name == 'zremrangebyscore':
                        for member, score in list(rows.items()):
                            if score <= values[1]: rows.pop(member)
                    elif name == 'zrange':
                        results.append(list(rows))
                        continue
                    elif name == 'zcard':
                        results.append(len(rows))
                        continue
                    else: raise AssertionError(name)
                    results.append(0)
                return results
        return Pipeline()


class StalledRedis(MemoryRedis):
    """在取得连接或执行命令时卡住，观察超时是否取消底层操作。"""
    def __init__(self, stage='execute'):
        super().__init__()
        self.stage = stage
        self.finished = 0

    async def stall(self):
        try:
            await asyncio.Event().wait()
        finally:
            self.finished += 1

    @asynccontextmanager
    async def pipeline(self, *, transaction):
        async with super().pipeline(transaction=transaction) as pipe:
            if self.stage == 'enter':
                await self.stall()
            pipe.execute = self.stall
            yield pipe


@pytest.mark.parametrize('stage', ['enter', 'execute'])
@pytest.mark.parametrize('operation', ['mark_waiting', 'clear_waiting'])
def test_display_timeout_covers_connection_and_command(app_env, monkeypatch, caplog, stage, operation):
    from alphapulse.ai import recovery
    redis = StalledRedis(stage)
    monkeypatch.setattr(recovery, 'redis', redis)
    monkeypatch.setattr(recovery, 'DISPLAY_TIMEOUT', 0.01)
    wait = recovery.RetryWait('scope', 'id', 1900, 3, True)

    async def run():
        await asyncio.wait_for(app_env[operation](wait), 0.5)
    asyncio.run(run())
    assert redis.finished == 1
    assert any(record.exc_info and isinstance(record.exc_info[1], TimeoutError) for record in caplog.records)


def test_stalled_display_does_not_prevent_retry_or_success(app_env, monkeypatch):
    from alphapulse.ai import llm, recovery
    redis = StalledRedis()
    monkeypatch.setattr(recovery, 'redis', redis)
    monkeypatch.setattr(recovery, 'DISPLAY_TIMEOUT', 0.01)
    for name in ('mark_waiting', 'clear_waiting'):
        monkeypatch.setattr(recovery, name, app_env[name])
    calls = []
    async def adapter(*args):
        calls.append(args)
        if len(calls) == 1:
            raise httpx.ConnectError('original transport error')
        yield llm.Completion(text='done', usage={'input_tokens': 10})
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, '_wait_local', AsyncMock())
    async def run():
        async def consume():
            return [x async for x in llm.stream(N(protocol='openai'), '', [], [])]
        return await asyncio.wait_for(consume(), 0.5)
    result = asyncio.run(run())
    assert len(calls) == 2 and result[-1].usage == {'input_tokens': 10}
    assert redis.finished == 2


def test_status_registration_and_consumption_count_towards_retry_delay(app_env, monkeypatch):
    from alphapulse.ai import llm, recovery
    clock, sends, sleeps = [0], [], []
    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(llm, 'asyncio', N(get_running_loop=lambda: N(time=lambda: clock[0]), sleep=sleep))
    async def mark(wait):
        clock[0] += 2
    monkeypatch.setattr(recovery, 'mark_waiting', mark)
    async def adapter(*args):
        sends.append(clock[0])
        if len(sends) == 1:
            raise httpx.ConnectError('offline')
        yield llm.Completion(text='done')
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    async def run():
        events = llm.stream(N(protocol='openai'), '', [], [])
        assert isinstance(await anext(events), llm.RetryStatus)
        clock[0] += 2
        assert [event async for event in events][-1].text == 'done'
    asyncio.run(run())
    assert sends == [0, 5] and sleeps == [1]


def test_wait_status_aggregation_refresh_expiry_and_own_cleanup(app_env, monkeypatch):
    from alphapulse.ai import recovery
    fake, clock = MemoryRedis(), [1000]
    monkeypatch.setattr(recovery, 'redis', fake)
    monkeypatch.setattr(recovery.time, 'time', lambda: clock[0])
    for name in ('availability', 'mark_waiting', 'clear_waiting', 'waiting_count'):
        monkeypatch.setattr(recovery, name, app_env[name])
    a = recovery.RetryWait('shared', 'a', 1900, 3, True)
    b = recovery.RetryWait('shared', 'b', 1005, 0, False)
    c = recovery.RetryWait('other', 'c', 1180, 1, True)
    async def run():
        for wait in (a, b, c): await recovery.mark_waiting(wait)
        assert await recovery.availability('shared') == dict(state='waiting', retry_at=1005, level=3)
        assert await recovery.waiting_count() == 2
        clock[0] += 30
        await recovery.mark_waiting(a)
        assert len(fake.rows[a.key]) == 2 and fake.rows[a.key][a.member] == 1120
        await recovery.clear_waiting(b)
        assert await recovery.availability('shared') == dict(state='waiting', retry_at=1900, level=3)
        assert await recovery.waiting_count() == 2
        clock[0] = 1091
        assert await recovery.availability('other') == dict(state='normal', retry_at=None, level=0)
        assert await recovery.waiting_count() == 1
        await recovery.clear_waiting(a)
        assert await recovery.availability('shared') == dict(state='normal', retry_at=None, level=0)
        assert await recovery.waiting_count() == 0
    asyncio.run(run())
    assert set(fake.ttls.values()) == {90}


def test_display_redis_failure_does_not_change_retry_or_response(app_env, monkeypatch):
    from alphapulse.ai import llm, recovery
    monkeypatch.setattr(recovery, 'redis', N(pipeline=lambda **kwargs: (_ for _ in ()).throw(RedisError('offline'))))
    for name in ('mark_waiting', 'clear_waiting'):
        monkeypatch.setattr(recovery, name, app_env[name])
    calls = []
    async def adapter(*args):
        calls.append(args)
        if len(calls) == 1: raise httpx.ConnectError('original transport error')
        yield llm.Completion(text='done', usage={'input_tokens': 10})
    monkeypatch.setattr(llm, '_stream_openai', adapter)
    monkeypatch.setattr(llm, '_wait_local', AsyncMock())
    async def run():
        return [x async for x in llm.stream(N(protocol='openai'), '', [], [])]
    result = asyncio.run(run())
    assert len(calls) == 2 and result[-1].usage == {'input_tokens': 10}
    monkeypatch.setattr(recovery, 'availability', app_env['availability'])
    with pytest.raises(llm.LlmError, match='无法读取模型重试等待状态'):
        asyncio.run(llm.availability(N(protocol='openai')))
    with pytest.raises(RedisError):
        asyncio.run(app_env['waiting_count']())


def test_retry_scope_only_groups_protocol_endpoint_and_credential(app_env, monkeypatch):
    from alphapulse.ai import llm
    monkeypatch.setattr(llm, 'decrypt', lambda value: value)
    scope = app_env['retry_scope']
    first = N(protocol='openai', base_url='https://example.invalid/v1/', api_key_encrypted='synthetic')
    second = N(protocol='openai', base_url='https://example.invalid', api_key_encrypted='synthetic')
    assert scope(first) == scope(second) and len(scope(first)) == 64
    second.api_key_encrypted = 'another-synthetic'
    assert scope(first) != scope(second)
    second.api_key_encrypted, second.protocol = 'synthetic', 'anthropic'
    assert scope(first) != scope(second)
