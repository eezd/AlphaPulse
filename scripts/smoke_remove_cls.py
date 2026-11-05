"""Verify retirement and full news-AI reset only inside the disposable CI database."""
import asyncio
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, MetaData, String, insert, text
from sqlalchemy.dialects.postgresql import JSONB

from alphapulse.core.config import get_settings
from alphapulse.core.db import engine
from alphapulse.crawler.remove_cls import AI_TABLES, OPTIONAL_AI_TABLES, cleanup
from alphapulse.models import NewsItem, Source
import alphapulse.news_research.models  # noqa: F401


async def main():
    assert get_settings().database_url.startswith('postgresql+asyncpg://ci:ci-only@')
    assert get_settings().database_url.endswith('/ci')
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                assert not await connection.scalar(text("SELECT count(*) FROM sources WHERE code IN ('cls_depth','cls_telegraph')"))
                await connection.execute(insert(Source).values(code='cls_depth', name='CI CLS', list_url='https://example.com', interval_seconds=120))
                for identity, source in ((9000001, 'cls_depth'), (9000002, 'wscn_live')):
                    await connection.execute(insert(NewsItem).values(id=identity, source_code=source, external_id='cleanup-ci',
                        url='https://example.com', published_at=datetime.now(UTC), content_hash=str(identity)))
                assert set((await cleanup(connection))['absent_legacy_tables']) == set(OPTIONAL_AI_TABLES)
                # These archived tables have no runtime ORM models; exercise their
                # optional cleanup with their historical schema in the disposable DB.
                await connection.execute(text('''CREATE TABLE news_ai_event_evidence (
                    id BIGSERIAL PRIMARY KEY,
                    version_id BIGINT NOT NULL REFERENCES news_ai_event_versions(id),
                    data JSONB NOT NULL, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now())'''))
                await connection.execute(text('''CREATE TABLE news_ai_event_corrections (
                    id BIGSERIAL PRIMARY KEY, fingerprint VARCHAR(64) UNIQUE NOT NULL,
                    input JSONB NOT NULL, result JSONB NOT NULL)'''))
                reflected = MetaData()
                await connection.run_sync(lambda sync: reflected.reflect(sync, only=list((*AI_TABLES, *OPTIONAL_AI_TABLES))))
                for table in reflected.sorted_tables:
                    if table.name not in (*AI_TABLES, *OPTIONAL_AI_TABLES):
                        continue
                    values = {}
                    for column in table.columns:
                        if not column.primary_key and (column.nullable or column.default is not None or column.server_default is not None):
                            continue
                        if isinstance(column.type, Integer):
                            values[column.name] = 9000001
                        elif isinstance(column.type, DateTime):
                            values[column.name] = datetime.now(UTC)
                        elif isinstance(column.type, String):
                            values[column.name] = 'ci'
                        elif isinstance(column.type, JSONB):
                            values[column.name] = {}
                        else:
                            raise AssertionError(column)
                    await connection.execute(table.insert().values(**values))
                await connection.execute(text("INSERT INTO raw_bodies VALUES (:shared,:body),(:exclusive,:body)"),
                    {'shared': 'a'*64, 'exclusive': 'b'*64, 'body': b'ci'})
                for identity, source, digest in ((9000001,'cls_depth','a'*64),(9000002,'wscn_live','a'*64),(9000003,'cls_depth','b'*64)):
                    await connection.execute(text('''INSERT INTO raw_responses
                        (id,source_code,url,method,status,headers,body_sha256) VALUES
                        (:id,:source,'https://example.com','GET',200,'{}',:digest)'''),
                        {'id':identity,'source':source,'digest':digest})
                preview = await cleanup(connection)
                assert not preview['executed'] and preview['counts']['sources'] == 1
                savepoint = await connection.begin_nested()
                await cleanup(connection, True)
                await savepoint.rollback()
                assert await connection.scalar(text("SELECT count(*) FROM news_items WHERE id=9000001")) == 1
                result = await cleanup(connection, True)
                assert result['executed']
                for table in (*AI_TABLES, *OPTIONAL_AI_TABLES):
                    assert await connection.scalar(text('SELECT count(*) FROM '+table)) == 0, table
                assert await connection.scalar(text("SELECT count(*) FROM news_items WHERE id=9000001")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM news_items WHERE id=9000002")) == 1
                assert await connection.scalar(text("SELECT count(*) FROM raw_bodies WHERE sha256=:digest"), {'digest':'a'*64}) == 1
                assert await connection.scalar(text("SELECT count(*) FROM raw_bodies WHERE sha256=:digest"), {'digest':'b'*64}) == 0
                assert (await cleanup(connection, True))['counts']['sources'] == 0
                print('CLS removal: preview, rollback, shared bodies, retained originals, 29 AI tables and repeat execution passed')
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


asyncio.run(main())
