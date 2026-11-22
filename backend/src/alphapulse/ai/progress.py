"""Transient execution progress and exact-wait wakeups for news tasks."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import json
import logging
import time
import uuid

from redis.exceptions import RedisError
from alphapulse.core.redis import KEY_PREFIX, redis

current = ContextVar('news_ai_progress', default=None)
active = {}
logger = logging.getLogger(__name__)
SNAPSHOT_KEY = KEY_PREFIX + 'ai:news-queue:progress'
WAKE_PREFIX = KEY_PREFIX + 'ai:news-queue:wake:'
IO_TIMEOUT = 0.1
SNAPSHOT_TTL = 10
FRESH_SECONDS = 5
FIELDS = ('task_id', 'claim', 'stage', 'object_id', 'phase', 'operation', 'started_at',
          'updated_at', 'model', 'reasoning_effort', 'round', 'tool', 'current_news_id',
          'retry_attempt', 'retry_at', 'wait_id', 'report_id', 'part_key', 'configuration')
last_success = {}


@contextmanager
def model_request(provider):
    record = current.get()
    if record is None:
        yield
        return
    key = f'{provider.id}:{provider.model}'
    record.update(configuration=key, _request_started=time.monotonic(), _request_active=True)
    try:
        yield
    finally:
        record['_request_active'] = False


def request_succeeded(provider):
    record = current.get()
    if record is not None and record.get('_request_active'):
        last_success[record['configuration']] = time.monotonic()


def upstream_failures(now=None):
    now = time.monotonic() if now is None else now
    groups = {}
    for record in active.values():
        if record.get('_request_active'):
            groups.setdefault(record['configuration'], []).append(record)
    return {key: records for key, records in groups.items() if all(record.get('wait_id') for record in records)
            and now - max(last_success.get(key, 0), min(record['_request_started'] for record in records)) >= 600}


def step(phase, **details):
    record = current.get()
    if record is not None:
        record.update(phase=phase, updated_at=datetime.now(UTC).isoformat(), **details)


def bind_claim(kind, identifier):
    record = current.get()
    if record is not None:
        record['claim'] = {'kind': kind, 'id': identifier}


@contextmanager
def retry_wait(attempt, retry_at):
    record = current.get()
    if record is None:
        yield None
        return
    signal = asyncio.Event()
    wait_id = uuid.uuid4().hex
    step('等待模型重试', retry_attempt=attempt, retry_at=retry_at, wait_id=wait_id,
         _wake_event=signal)
    try:
        yield signal
    finally:
        if record.get('wait_id') == wait_id:
            step('准备重试', retry_at=None, wait_id=None, _wake_event=None)


async def execute(key, action):
    identity = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    record = {'task_id': identity, 'stage': 'research' if key[0] == 'event' else key[0], 'object_id': key[1],
              'phase': '准备输入', 'operation': '准备输入', 'started_at': now,
              'updated_at': now, 'retry_attempt': 0}
    active[identity] = record
    token = current.set(record)
    try:
        return await action
    finally:
        current.reset(token)
        active.pop(identity, None)


async def sync_once(worker_id):
    # Capture identities before I/O; a wait may finish while Redis responds.
    records = list(active.values())
    waits = [(r, r['wait_id'], r['_wake_event']) for r in records if r.get('wait_id')]
    snapshot = {'worker_id': worker_id, 'heartbeat_at': datetime.now(UTC).isoformat(),
                'tasks': [{key: r.get(key) for key in FIELDS} for r in records]}
    try:
        async with asyncio.timeout(IO_TIMEOUT), redis.pipeline(transaction=True) as pipe:
            for _, wait_id, _ in waits:
                pipe.get(WAKE_PREFIX + wait_id)
            pipe.set(SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False), ex=SNAPSHOT_TTL)
            results = await pipe.execute()
        for (record, wait_id, signal), command in zip(waits, results[:-1], strict=True):
            if (active.get(record['task_id']) is record and record.get('wait_id') == wait_id
                    and command == record['task_id']):
                signal.set()
    except (RedisError, TimeoutError):
        logger.warning('无法同步新闻 AI 队列进度或立即重试命令', exc_info=True)


async def report():
    worker_id = uuid.uuid4().hex
    while True:
        await sync_once(worker_id)
        await asyncio.sleep(1)


async def read_snapshot(*, strict=False):
    """Return an explicit freshness status; Redis failure never means an empty queue."""
    try:
        async with asyncio.timeout(IO_TIMEOUT):
            raw = await redis.get(SNAPSHOT_KEY)
        if raw is None:
            return 'unavailable', None
        snapshot = json.loads(raw)
        heartbeat = datetime.fromisoformat(snapshot['heartbeat_at'])
        age = (datetime.now(UTC) - heartbeat).total_seconds()
        if not isinstance(snapshot['tasks'], list):
            raise ValueError('队列进度 tasks 必须为列表')
        return ('available' if 0 <= age <= FRESH_SECONDS else 'stale'), snapshot
    except (RedisError, TimeoutError, ValueError, TypeError, KeyError):
        logger.warning('无法读取新闻 AI 队列进度', exc_info=True)
        if strict:
            raise
        return 'unavailable', None
