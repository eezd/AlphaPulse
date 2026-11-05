"""本次采集修复初始化：默认只读，--execute 幂等建立一次新预算。运行前停止 crawler。"""
import argparse
import asyncio
import copy
import json
from datetime import UTC, datetime

from sqlalchemy import select, text

from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import Source

REPAIR = 'collection-repair-20261105-v1'
CODES = ('sec', 'doj', 'congress')


async def run(execute=False):
    output = []
    async with SessionLocal() as session:
        if not execute:
            await session.execute(text('SET TRANSACTION READ ONLY'))
        query = select(Source).where(Source.code.in_(CODES)).order_by(Source.code)
        if execute:
            query = query.with_for_update()
        for source in await session.scalars(query):
            cursor = copy.deepcopy(source.history_cursor or {})
            if cursor.get('collection_repair') == REPAIR:
                output.append({'source': source.code, 'already_initialized': True})
                continue
            affected = []
            for key in ('document_retries', 'attachment_retries', 'bill_retries'):
                for url, entry in cursor.get(key, {}).items():
                    affected.append({'kind': key, 'url': url, 'previous_attempts': entry['attempts']})
                    entry['repair_previous'] = {k: v for k, v in entry.items() if k != 'document'}
                    entry['attempts'] = 0
                    entry['next_at'] = datetime.now(UTC).timestamp()
                    entry.pop('retired_at', None)
            cursor['collection_repair'] = REPAIR
            if execute:
                source.history_cursor = cursor
            output.append({'source': source.code, 'items': affected})
        if execute:
            await session.commit()
    await engine.dispose()
    return {'mode': 'execute' if execute else 'dry-run', 'sources': output}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    print(json.dumps(asyncio.run(run(parser.parse_args().execute)), ensure_ascii=False))
