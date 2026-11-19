"""按请求展示模型重试等待；不参与生成请求准入或重试决策。"""
import asyncio
from dataclasses import dataclass
import json
import logging
import time

from redis.exceptions import RedisError
from alphapulse.core.redis import KEY_PREFIX, redis

WAIT_TTL = 90
DISPLAY_TIMEOUT = 0.1
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryWait:
    scope: str
    request_id: str
    retry_at: float
    level: int
    news: bool

    @property
    def key(self):
        return KEY_PREFIX + 'ai:retry:waiting:' + self.scope

    @property
    def member(self):
        return json.dumps({'request_id': self.request_id, 'retry_at': self.retry_at,
                           'level': self.level}, sort_keys=True)


async def mark_waiting(wait: RetryWait):
    try:
        async with asyncio.timeout(DISPLAY_TIMEOUT), redis.pipeline(transaction=True) as pipe:
            pipe.zadd(wait.key, {wait.member: time.time() + WAIT_TTL})
            pipe.expire(wait.key, WAIT_TTL)
            if wait.news:
                key = KEY_PREFIX + 'ai:news-waiting'
                pipe.zadd(key, {wait.request_id: time.time() + WAIT_TTL})
                pipe.expire(key, WAIT_TTL)
            await pipe.execute()
    except (RedisError, TimeoutError):
        logger.warning('无法更新模型重试等待状态', exc_info=True)


async def clear_waiting(wait: RetryWait):
    try:
        async with asyncio.timeout(DISPLAY_TIMEOUT), redis.pipeline(transaction=True) as pipe:
            pipe.zrem(wait.key, wait.member)
            if wait.news:
                pipe.zrem(KEY_PREFIX + 'ai:news-waiting', wait.request_id)
            await pipe.execute()
    except (RedisError, TimeoutError):
        logger.warning('无法清理模型重试等待状态', exc_info=True)


async def availability(scope):
    key = KEY_PREFIX + 'ai:retry:waiting:' + scope
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, '-inf', time.time())
        pipe.zrange(key, 0, -1)
        _, members = await pipe.execute()
    rows = [json.loads(member) for member in members]
    return {'state': 'waiting' if rows else 'normal',
            'retry_at': min(row['retry_at'] for row in rows) if rows else None,
            'level': max(row['level'] for row in rows) if rows else 0}


async def waiting_count():
    key = KEY_PREFIX + 'ai:news-waiting'
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, '-inf', time.time())
        pipe.zcard(key)
        _, count = await pipe.execute()
    return count
