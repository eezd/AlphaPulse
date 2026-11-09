"""Remove only retired trading Redis keys, after all application writers stop."""
import argparse
import asyncio
import json

from sqlalchemy import text

from alphapulse.core.db import engine
from alphapulse.core.redis import redis

PATTERNS = ('ap:trader:*', 'ap:trading:*')


async def cleanup(client, execute=False):
    result = {'executed': execute, 'matched': 0, 'removed': 0}
    for pattern in PATTERNS:
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
            result['matched'] += len(keys)
            if execute and keys:
                result['removed'] += await client.unlink(*keys)
            if cursor == 0:
                break
    return result


async def main(execute=False):
    try:
        if execute:
            # Do not delete a running legacy worker's state before its schema is retired.
            async with engine.connect() as connection:
                if await connection.scalar(text("SELECT to_regclass('public.trading_account')")):
                    raise RuntimeError('Retire the trading schema and stop all writers before Redis cleanup')
        print(json.dumps(await cleanup(redis, execute)))
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    asyncio.run(main(parser.parse_args().execute))
