from redis.asyncio import Redis

from alphapulse.core.config import get_settings

# DB 14 与 AFFiNE 共用：所有 key 必须带此前缀，禁止 FLUSHDB
KEY_PREFIX = "ap:"

redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
