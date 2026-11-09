"""Exercise populated retirement only before API startup in the disposable CI DB."""
import asyncio
from datetime import UTC, datetime
import os
import subprocess

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, MetaData, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB

from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.core.remove_trading import cleanup
from alphapulse.models import AiProvider, ChatMessage, ChatSession, ContextVersion, MarketSymbol, NewsItem, SkillNode, SkillVersion
from alphapulse.news_research.models import Event, Membership, Package, Version

TABLES = ('trading_orders', 'trading_events', 'trading_fills', 'trading_symbols',
          'trading_credentials', 'trading_account', 'trading_analyses', 'news_signals')
IDENTITY = 9100100


def migrate(*args, succeeds=True):
    result = subprocess.run(['alembic', *args], capture_output=True, text=True)
    assert (result.returncode == 0) == succeeds, result.stderr
    if not succeeds:
        assert 'downgrade is unsupported' in result.stderr


async def fingerprints(connection):
    names = list(await connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")))
    result = {}
    for name in names:
        if name in (*TABLES, 'alembic_version'):
            continue
        quoted = connection.dialect.identifier_preparer.quote(name)
        result[name] = (await connection.execute(text(
            "SELECT count(*), md5(coalesce(string_agg(value,'' ORDER BY value),'')) FROM "
            '(SELECT md5(row_to_json(r)::text) AS value FROM ' + quoted + ' r) rows'
        ))).one()
    return result


async def main():
    assert os.environ['DATABASE_URL'].startswith('postgresql+asyncpg://ci:ci-only@ap-ci-')
    assert os.environ['DATABASE_URL'].endswith('/ci')
    try:
        migrate('upgrade', 'ef6789012345')
        async with engine.connect() as connection:
            baseline = await fingerprints(connection)
        async with SessionLocal() as session:
            session.add(AiProvider(id=IDENTITY, name='retirement-ci', protocol='openai', base_url='https://example.invalid',
                                   api_key_encrypted=b'ci-only', api_key_hint='only', model='ci'))
            session.add(ChatSession(id=IDENTITY))
            session.add(ContextVersion(id=IDENTITY, content='保留上下文', source='user'))
            session.add(SkillNode(id=IDENTITY, kind='skill', scope='shared', name='保留共享规则'))
            session.add(NewsItem(id=IDENTITY, source_code='wscn_news', external_id='retirement-ci',
                                 url='https://example.invalid', title='原始新闻', content_text='Original text',
                                 published_at=datetime.now(UTC), content_hash='c' * 64))
            session.add(MarketSymbol(id=IDENTITY, market='um', symbol='CITESTUSDT', base='CITEST', quote='USDT',
                                     price_precision=2, volume_precision=2))
            await session.flush()
            session.add(ChatMessage(id=IDENTITY, session_id=IDENTITY, provider_id=IDENTITY, role='user', content={'text': '保留消息'}))
            session.add(SkillVersion(id=IDENTITY, skill_id=IDENTITY, version_no=1, source='user', content='保留规则'))
            session.add(Package(id=IDENTITY, news_id=IDENTITY, content_hash='c' * 64, state='done', keywords=[{'canonical': '保留标签'}]))
            session.add(Event(id=IDENTITY, title='保留事件', state='done', result={'summary': '保留结论'}))
            await session.flush()
            session.add(Membership(event_id=IDENTITY, package_id=IDENTITY, evidence='Original', reason='保留', change_kind='new_fact'))
            session.add(Version(id=IDENTITY, event_id=IDENTITY, revision=1, state='done', input={}, output={'summary': '保留版本'}, rules={}))
            await session.commit()
        async with engine.begin() as connection:
            metadata = MetaData()
            await connection.run_sync(lambda sync: metadata.reflect(sync, only=list(TABLES)))
            for table in metadata.sorted_tables:
                if table.name not in TABLES:
                    continue
                values = {}
                for column in table.columns:
                    if column.name in ('analysis_id', 'provider_id', 'news_id'):
                        values[column.name] = IDENTITY
                    elif column.primary_key or (not column.nullable and column.server_default is None):
                        if isinstance(column.type, Boolean):
                            value = False
                        elif isinstance(column.type, Integer):
                            value = IDENTITY
                        elif isinstance(column.type, Numeric):
                            value = 1
                        elif isinstance(column.type, DateTime):
                            value = datetime.now(UTC)
                        elif isinstance(column.type, LargeBinary):
                            value = b'ci-only'
                        elif isinstance(column.type, JSONB):
                            value = {}
                        elif isinstance(column.type, String):
                            value = 'ci'
                        else:
                            raise AssertionError(column)
                        values[column.name] = value
                if table.name == 'trading_credentials':
                    values.update(api_key_encrypted=b'ci-key', api_secret_encrypted=b'ci-secret', api_key_hint='ci')
                if table.name == 'trading_account':
                    values['id'] = 1
                await connection.execute(table.insert().values(**values))
                assert await connection.scalar(text('SELECT count(*) FROM ' + table.name)) > 0
            before = await fingerprints(connection)
        migrate('upgrade', 'head')
        async with engine.connect() as connection:
            for table in TABLES:
                assert await connection.scalar(text('SELECT to_regclass(:name)'), {'name': table}) is None
            assert await fingerprints(connection) == before
        migrate('downgrade', 'ef6789012345', succeeds=False)
        async with engine.connect() as connection:
            assert await connection.scalar(text('SELECT version_num FROM alembic_version')) == 'f07890123456'
            assert await fingerprints(connection) == before
        # Remove this smoke's own samples so subsequent queue tests keep their isolated fixtures.
        async with engine.begin() as connection:
            for table, column in [('news_research_memberships', 'event_id'), ('news_research_versions', 'id'),
                                  ('news_research_packages', 'id'), ('news_research_events', 'id'),
                                  ('skill_versions', 'id'), ('skill_nodes', 'id'), ('chat_messages', 'id'),
                                  ('chat_sessions', 'id'), ('context_versions', 'id'), ('market_symbols', 'id'),
                                  ('news_items', 'id'), ('ai_providers', 'id')]:
                await connection.execute(text('DELETE FROM ' + table + ' WHERE ' + column + '=:id'), {'id': IDENTITY})
            assert await fingerprints(connection) == baseline
        retained = {'ap:market:retirement-ci': 'market', 'ap:news:retirement-ci': 'news', 'other:retirement-ci': 'shared'}
        retired = {'ap:trader:status': 'old', 'ap:trading:symbols': 'old'}
        await redis.mset({**retained, **retired})
        assert (await cleanup(redis))['matched'] == len(retired)
        assert await redis.mget(list(retired)) == list(retired.values())
        assert (await cleanup(redis, True))['removed'] == len(retired)
        assert await redis.mget(list(retained)) == list(retained.values())
        assert (await cleanup(redis, True))['removed'] == 0
        await redis.unlink(*retained)
        print('Trading retirement: populated upgrade, retained fingerprints, rejected downgrade and scoped Redis cleanup passed')
    finally:
        await engine.dispose()
        await redis.aclose()


asyncio.run(main())
