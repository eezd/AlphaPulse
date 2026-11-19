"""Queue visibility and exact-wait commands, using synthetic I/O only."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, 'sqlite')
def json_sqlite(element, compiler, **kw):
    return 'JSON'


class QueueRedis:
    def __init__(self):
        self.values = {}
        self.commands = []
        self.fail = False
        self.stall = False

    async def get(self, key):
        if self.fail:
            from redis.exceptions import ConnectionError
            raise ConnectionError('isolated unavailable')
        if self.stall:
            await asyncio.Event().wait()
        return self.values.get(key)

    def pipeline(self, **kwargs):
        owner = self
        class Pipe:
            def __init__(self):
                self.calls = []
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, key):
                self.calls.append(('get', key, None, {}))
            def set(self, key, value, **options):
                self.calls.append(('set', key, value, options))
            async def execute(self):
                results = []
                for method, key, value, options in self.calls:
                    owner.commands.append((method, key, value, options))
                    if method == 'get':
                        results.append(await owner.get(key))
                    else:
                        await owner.get('__io_check__')
                        duplicate = options.get('nx') and key in owner.values
                        if not duplicate:
                            owner.values[key] = value
                        results.append(None if duplicate else True)
                return results
        return Pipe()


@pytest.fixture
def queue_env(app_env, monkeypatch):
    from alphapulse.ai import progress
    redis = QueueRedis()
    monkeypatch.setattr(progress, 'redis', redis)
    monkeypatch.setattr(progress, 'sync_once', app_env['sync_once'])
    assert not progress.active
    return progress, redis


@pytest.fixture
def queue_db(app_env):
    from alphapulse.models import NewsItem
    from alphapulse.briefings.models import Analysis, Report, ReportPart
    from alphapulse.core.db import Base
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (NewsItem, Analysis, Report, ReportPart)])
    with Session(engine) as db:
        now = datetime.now(UTC)
        db.add(NewsItem(id=101, source_code='synthetic', external_id='1', title='当前标题',
                        url='https://example.invalid/101', published_at=now,
                        content_hash='h', summary='', content_text='原文'))
        source = {'id': 101, 'title': '调用时新闻标题', 'published_at': now.isoformat(), 'url': 'https://example.invalid/old'}
        db.add(Analysis(id=31, news_id=101, version_no=1, state='processing', lane='live', effective_at=now,
                        time_basis='published_at', input=source, control_revision=1))
        db.add(Report(id=41, kind='morning', report_date=now.date(), version_no=1, state='processing', lane='live',
                      window_start=now, window_end=now, due_at=now, next_check_at=now,
                      input={'cards': [{'news_id': 101, 'source': source}]}, control_revision=1))
        db.commit()
        session = N(scalars=AsyncMock(side_effect=db.scalars), execute=AsyncMock(side_effect=db.execute))
        yield session, db
    engine.dispose()


def test_progress_context_claim_and_cleanup_are_isolated(queue_env):
    progress, _ = queue_env
    async def run():
        ready = asyncio.Event()
        async def action(identifier):
            progress.bind_claim('analysis', identifier)
            progress.step('查询资料', tool='read_news', current_news_id=identifier)
            if identifier == 1:
                ready.set()
                await asyncio.Event().wait()
            assert progress.current.get()['claim']['id'] == 2
            raise ValueError('synthetic failure')
        first = asyncio.create_task(progress.execute(('analysis', 1), action(1)))
        await ready.wait()
        with pytest.raises(ValueError, match='synthetic failure'):
            await progress.execute(('analysis', 2), action(2))
        assert len(progress.active) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not progress.active and progress.current.get() is None
    asyncio.run(run())


def test_sync_snapshot_and_old_commands_cannot_wake_next_wait(queue_env):
    progress, redis = queue_env
    async def action():
        progress.bind_claim('analysis', 31)
        with progress.retry_wait(6, datetime.now(UTC).isoformat()) as signal:
            record = progress.current.get()
            first_id = record['wait_id']
            await progress.sync_once('worker-one')
            status, snapshot = await progress.read_snapshot()
            assert status == 'available' and snapshot['tasks'][0]['claim']['id'] == 31
            assert '_wake_event' not in snapshot['tasks'][0]
            redis.values[progress.WAKE_PREFIX + first_id] = record['task_id']
            await progress.sync_once('worker-one')
            assert signal.is_set()
        with progress.retry_wait(7, datetime.now(UTC).isoformat()) as next_signal:
            assert record['wait_id'] != first_id
            await progress.sync_once('worker-one')
            assert not next_signal.is_set()
        assert record['wait_id'] is None
    asyncio.run(progress.execute(('analysis', 31), action()))
    assert all(options.get('ex') == 10 for method, _, _, options in redis.commands if method == 'set')


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('attempt', [1, 6, 8])
def test_manual_wakeup_preserves_context_and_next_retry_level(queue_env, monkeypatch, protocol, attempt):
    progress, _ = queue_env
    from alphapulse.ai import llm, recovery
    original_wait = llm._wait_local
    delays, calls = [], []
    messages = [{'role': 'user', 'text': 'keep original tools'}]
    async def adapter(provider, system, received, tools, effort):
        calls.append(received)
        if len(calls) <= attempt + 1:
            raise llm.httpx.ConnectError('synthetic')
        yield llm.Completion(usage={'tokens': 7})
    async def wait(delay, coordinate, *, started_at):
        delays.append(delay)
        record = progress.current.get()
        assert coordinate.news
        if len(delays) == attempt:
            record['_wake_event'].set()
            await original_wait(delay, coordinate, started_at=started_at)
    monkeypatch.setattr(llm, '_stream_' + protocol, adapter)
    monkeypatch.setattr(llm, '_wait_local', wait)
    async def action():
        events = [event async for event in llm.stream(N(protocol=protocol), '', messages, [])]
        assert events[-1].usage == {'tokens': 7}
        assert progress.current.get()['wait_id'] is None
    asyncio.run(progress.execute(('analysis', 31), action()))
    assert all(received is messages for received in calls)
    assert delays == [5, 5, 5, 5, 5, 180, 600, 900, 900][:attempt + 1]
    assert recovery.mark_waiting.await_count == attempt + 1
    assert recovery.clear_waiting.await_count == attempt + 1


def test_queue_projection_uses_snapshot_inputs_and_db_claims(queue_env, queue_db):
    progress, _ = queue_env
    from alphapulse.briefings import queue
    session, db = queue_db
    async def action():
        progress.bind_claim('analysis', 31)
        with progress.retry_wait(6, datetime.now(UTC).isoformat()):
            await progress.sync_once('worker')
            result = await queue.queue_view(session)
            assert len(result['items']) == 2
            item, report = result['items']
            assert item['news'][0]['title'] == '调用时新闻标题'
            assert item['news'][0]['source_code'] == 'synthetic'
            assert report['report']['id'] == 41
            assert report['news'] == []
            assert item['can_retry_now'] and report['progress'] is None
            assert not db.new and not db.dirty and not db.deleted
    asyncio.run(progress.execute(('analysis', 31), action()))


def test_reset_waits_precise_deduplicated_and_claim_revocation(queue_env, queue_db):
    progress, redis = queue_env
    from alphapulse.briefings import queue
    from alphapulse.briefings.models import Analysis
    session, db = queue_db
    async def action():
        progress.bind_claim('analysis', 31)
        with progress.retry_wait(6, datetime.now(UTC).isoformat()) as signal:
            await progress.sync_once('worker')
            record = progress.current.get()
            target = {k: record[k] for k in ('task_id', 'wait_id')}
            missing = {'task_id': uuid4().hex, 'wait_id': uuid4().hex}
            body = queue.ResetWaits(targets=[target, target, missing])
            result = await queue.reset_waits(session, body)
            assert result['scheduled'] == [target] and len(result['skipped']) == 1
            result = await queue.reset_waits(session, body)
            assert result['already_requested'] == [target]
            assert not signal.is_set()  # API only registers, worker consumes.
            await progress.sync_once('worker')
            assert signal.is_set()
            db.get(Analysis, 31).state = 'interrupted'
            db.commit()
            result = await queue.reset_waits(session, body)
            assert not result['scheduled'] and len(result['skipped']) == 2
            assert redis.values[progress.WAKE_PREFIX + target['wait_id']] == target['task_id']
    asyncio.run(progress.execute(('analysis', 31), action()))


@pytest.mark.parametrize('mode', ['expired', 'missing', 'failure', 'slow'])
def test_unavailable_progress_keeps_real_tasks_and_disables_reset(queue_env, queue_db, mode):
    progress, redis = queue_env
    from alphapulse.briefings import queue
    from fastapi import HTTPException
    session, _ = queue_db
    if mode == 'expired':
        redis.values[progress.SNAPSHOT_KEY] = json.dumps({'heartbeat_at': (datetime.now(UTC)-timedelta(seconds=6)).isoformat(), 'tasks': []})
    redis.fail = mode == 'failure'
    redis.stall = mode == 'slow'
    async def action():
        view = await queue.queue_view(session)
        assert len(view['items']) == 2 and all(not i['can_retry_now'] for i in view['items'])
        assert view['progress_status'] == ('stale' if mode == 'expired' else 'unavailable')
        if mode in ('failure', 'slow'):
            with pytest.raises(HTTPException) as error:
                await queue.reset_waits(session, queue.ResetWaits(targets=[{'task_id': uuid4(), 'wait_id': uuid4()}]))
            assert error.value.status_code == 503
    asyncio.run(action())


@pytest.mark.parametrize('mode', ['failure', 'slow'])
def test_progress_reporting_failure_never_blocks_cancel(queue_env, mode):
    progress, redis = queue_env
    redis.fail = mode == 'failure'
    redis.stall = mode == 'slow'
    async def action():
        await asyncio.wait_for(progress.sync_once('worker'), .3)
        task = asyncio.create_task(progress.report())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .3)
        assert progress.current.get() is None
    asyncio.run(action())


def test_analysis_progress_matches_claim_and_source_metadata_can_disappear(queue_env, queue_db):
    progress, redis = queue_env
    from alphapulse.briefings import queue
    from alphapulse.models import NewsItem
    session, db = queue_db
    db.delete(db.get(NewsItem, 101))
    db.commit()
    async def action():
        progress.bind_claim('analysis', 31)
        progress.step('单篇分析', operation='单篇分析', current_news_id=101)
        with progress.retry_wait(8, datetime.now(UTC).isoformat()):
            await progress.sync_once('worker')
            view = await queue.queue_view(session)
            item = next(i for i in view['items'] if i['claim']['kind'] == 'analysis')
            assert item['can_retry_now'] and item['stage'] == 'analysis'
            assert item['progress']['current_news_id'] == 101
            assert item['news'][0]['title'] == '调用时新闻标题'
            assert item['news'][0]['source_code'] == '来源不可用'
            # A new worker cannot use a snapshot whose claim has the wrong parent identity.
            snapshot = json.loads(redis.values[progress.SNAPSHOT_KEY])
            snapshot['tasks'][0]['object_id'] = 999
            redis.values[progress.SNAPSHOT_KEY] = json.dumps(snapshot)
            view = await queue.queue_view(session)
            assert all(not i['can_retry_now'] for i in view['items'])
    asyncio.run(progress.execute(('analysis', 31), action()))


@pytest.mark.parametrize('mode', ['failure', 'slow', 'partial'])
def test_command_write_failure_returns_503_without_touching_claims(queue_env, queue_db, monkeypatch, mode):
    progress, redis = queue_env
    from alphapulse.briefings import queue
    from alphapulse.briefings.models import Analysis
    from fastapi import HTTPException
    session, db = queue_db
    async def action():
        progress.bind_claim('analysis', 31)
        with progress.retry_wait(6, datetime.now(UTC).isoformat()) as signal:
            await progress.sync_once('worker')
            record = progress.current.get()
            body = queue.ResetWaits(targets=[{k: record[k] for k in ('task_id', 'wait_id')}])
            original = queue.queue_view
            async def read_then_fail(*args, **kwargs):
                result = await original(*args, **kwargs)
                if mode == 'partial':
                    # Simulate SET reaching Redis before the reply is lost.
                    redis.values[progress.WAKE_PREFIX + record['wait_id']] = record['task_id']
                redis.fail = mode != 'slow'
                redis.stall = mode == 'slow'
                return result
            monkeypatch.setattr(queue, 'queue_view', read_then_fail)
            with pytest.raises(HTTPException) as error:
                await queue.reset_waits(session, body)
            assert error.value.status_code == 503
            assert '结果未确认' in error.value.detail
            assert db.get(Analysis, 31).state == 'processing' and not signal.is_set()
            assert not db.new and not db.dirty and not db.deleted
    asyncio.run(progress.execute(('analysis', 31), action()))


def test_queue_api_auth_and_strict_validation(queue_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from alphapulse.briefings import api, queue
    from alphapulse.core.auth import require_admin
    from alphapulse.core.db import get_session
    app = FastAPI()
    app.include_router(api.router)
    async def session():
        yield N()
    app.dependency_overrides[get_session] = session
    client = TestClient(app)
    assert client.get('/api/briefings/queue').status_code == 401
    app.dependency_overrides[require_admin] = lambda: 'admin'
    monkeypatch.setattr(queue, 'queue_view', AsyncMock(return_value={'items': []}))
    assert client.get('/api/briefings/queue').json()['data'] == {'items': []}
    for body in ({'targets': []}, {'targets': [{'task_id': 'invalid', 'wait_id': uuid4().hex}]},
                 {'targets': [{'task_id': uuid4().hex, 'wait_id': uuid4().hex, 'extra': 1}]},
                 {'targets': [{'task_id': uuid4().hex, 'wait_id': uuid4().hex} for _ in range(17)]}):
        assert client.post('/api/briefings/queue/reset-waits', json=body).status_code == 422
