"""在明确指定的隔离 Redis 上验证请求等待展示，不访问生成接入。"""
import asyncio
import os
import uuid


def main():
    from redis.asyncio import Redis
    from alphapulse.ai import recovery

    async def run():
        client = Redis.from_url(os.environ['AI_RECOVERY_TEST_REDIS_URL'], decode_responses=True)
        identifier = uuid.uuid4().hex
        original_client, original_prefix = recovery.redis, recovery.KEY_PREFIX
        recovery.redis = client
        recovery.KEY_PREFIX = 'ap:ai:test:' + identifier + ':'
        first = recovery.RetryWait('shared', 'first', 1900, 3, True)
        second = recovery.RetryWait('shared', 'second', 1005, 0, False)
        other = recovery.RetryWait('other', 'other', 1180, 1, True)
        keys = [first.key, other.key, recovery.KEY_PREFIX + 'ai:news-waiting']
        try:
            await asyncio.gather(*(recovery.mark_waiting(wait) for wait in (first, second, other)))
            assert await recovery.availability('shared') == dict(state='waiting', retry_at=1005, level=3)
            assert await recovery.waiting_count() == 2
            await recovery.mark_waiting(first)
            assert await client.zcard(first.key) == 2
            assert 0 < await client.ttl(first.key) <= recovery.WAIT_TTL
            await recovery.clear_waiting(second)
            assert await recovery.availability('shared') == dict(state='waiting', retry_at=1900, level=3)
            # 陈旧展示记录按 score 清理，不依赖集合 TTL 已到期。
            await client.zadd(other.key, {other.member: 0})
            await client.zadd(keys[-1], {other.request_id: 0})
            assert await recovery.availability('other') == dict(state='normal', retry_at=None, level=0)
            assert await recovery.waiting_count() == 1
            await recovery.clear_waiting(first)
            assert await recovery.availability('shared') == dict(state='normal', retry_at=None, level=0)
            assert await recovery.waiting_count() == 0
        finally:
            await client.delete(*keys)
            recovery.redis, recovery.KEY_PREFIX = original_client, original_prefix
            await client.aclose()
    asyncio.run(run())


if __name__ == '__main__':
    main()
    print('AI retry status acceptance passed')
