"""词表查询在独占 PostgreSQL 上的验收；不读取生产配置。"""
import asyncio
import os

if os.environ.get('BRIEFINGS_TEST_ISOLATED') != '1':
    raise RuntimeError('必须显式使用独占测试资源')
for key in ('DATABASE_URL', 'REDIS_URL'):
    os.environ[key] = os.environ['BRIEFINGS_TEST_' + key]

from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB
from alphapulse.briefings.keywords import key
from alphapulse.briefings.queries import keyword_match, keyword_name_key
from alphapulse.core.db import SessionLocal, engine


async def main():
    table = Table('keyword_search_ci', MetaData(), Column('id', Integer), Column('keywords', JSONB))
    rows = [
        (1, '高盛研究', ['Goldman Sachs Research']),
        (2, '高盛研究部', ['高盛研究部']),
        (3, '高盛集团', ['Goldman Sachs']),
        (4, '高盛执行办公室', ['Goldman Sachs Executive Office']),
        (5, '美元', ['美元']),
        (6, 'USD', ['US dollars']),
        (7, '以太坊', ['ETH']),
        (8, 'ETHUSDT', ['ETHUSDT']),
        (9, 'wstETH', ['wstETH']),
        (10, '发行注册', ['发行注册']),
        (11, '字面%_测试', ['字面%_测试']),
        (14, 'Goldman  Sachs Research', ['Goldman  Sachs Research']),
        (15, '历史研究名称', ['Goldman\nSachs\tResearch']),
        (16, 'ＮＶＩＤＩＡ', ['ＮＶＩＤＩＡ']),
        (17, '历史公司名称', ['\u00a0ＮＶＩＤＩＡ\u00a0']),
        (18, 'Goldman\tSachs', ['Goldman\tSachs']),
        (19, 'Goldman Sachs\nExecutive Office', ['Goldman Sachs\nExecutive Office']),
    ]
    try:
        async with SessionLocal() as session:
            for value in ('Straße', 'ΟΣ', ' ＮＶＩＤＩＡ\u00a0', 'Goldman\n\tSachs Research', ' A\x1cB\x85C '):
                assert await session.scalar(select(keyword_name_key(value))) == key(value), repr(value)
            await session.execute(text('CREATE TEMP TABLE keyword_search_ci (id integer, keywords jsonb) ON COMMIT DROP'))
            await session.execute(insert(table), [
                {'id': identifier, 'keywords': [{'canonical': canonical, 'aliases': aliases, 'kind': 'topic', 'quote': aliases[0]}]}
                for identifier, canonical, aliases in rows
            ] + [{'id': 12, 'keywords': []}, {'id': 13, 'keywords': None}])
            for query, expected in [
                ('高盛研究', {1, 2, 14, 15}), ('Goldman Sachs Research', {1, 2, 14, 15}),
                ('高盛研究部', {1, 2, 14, 15}), ('Goldman  Sachs Research', {1, 2, 14, 15}),
                ('Goldman\nSachs\tResearch', {1, 2, 14, 15}),
                ('Goldman Sachs', {3, 18}), ('高盛执行办公室', {4, 19}),
                ('ＮＶＩＤＩＡ', {16, 17}), ('NVIDIA', {16, 17}), ('英伟达', {16, 17}),
                ('USD', {5, 6}), ('美元', {5, 6}), ('eth', {7}), ('以太坊', {7}),
                ('发行', {10}), ('%_', {11}), ("' OR 1=1 --", set()),
            ]:
                found = set(await session.scalars(select(table.c.id).where(keyword_match(table.c.keywords, query))))
                assert found == expected, (query, found, expected)
            await session.rollback()
        print('Briefing keyword SQL acceptance passed: synonyms, historical results, divisions and literal boundaries', flush=True)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
