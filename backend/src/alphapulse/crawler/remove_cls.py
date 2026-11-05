"""Fixed-scope operator cleanup; callers must stop all application writers."""

import argparse
import asyncio
import json

from sqlalchemy import text

from alphapulse.core.db import engine

SOURCES = ('cls_depth', 'cls_telegraph')
AI_TABLES = (
    'news_research_batch_events', 'news_research_batch_packages', 'news_research_batches',
    'news_research_references', 'news_research_versions', 'news_research_memberships',
    'news_research_attempts', 'news_research_packages', 'news_research_events',
    'news_ai_reprocess_targets', 'news_ai_reprocess_items', 'news_ai_reprocess_jobs',
    'news_ai_event_links',
    'news_ai_event_versions', 'news_ai_event_members', 'news_ai_events',
    'news_ai_package_corrections', 'news_package_evidence', 'news_ai_summary_versions',
    'news_ai_grouping_versions', 'news_ai_summary_articles', 'news_ai_summaries',
    'news_ai_article_analyses', 'risk_snapshots', 'risk_analyses', 'risk_events', 'news_signals',
)
# These retired models were never included in the migration chain.
OPTIONAL_AI_TABLES = ('news_ai_event_evidence', 'news_ai_event_corrections')
SOURCE_TABLES = ('news_items', 'raw_responses', 'crawl_logs', 'crawl_runs', 'sources')


async def retained_news(connection):
    return (await connection.execute(text('''
        SELECT count(*), md5(coalesce(string_agg(id::text || ':' || content_hash, ',' ORDER BY id), ''))
        FROM news_items WHERE NOT (source_code = ANY(:sources))
    '''), {'sources': list(SOURCES)})).one()._asdict()


async def protected_records(connection):
    tables = list(await connection.scalars(text("""
        SELECT tablename FROM pg_tables WHERE schemaname='public' AND
        (tablename IN ('ai_providers','skill_nodes','skill_versions',
         'news_research_vocabulary_versions','chat_sessions','chat_messages','context_versions')
         OR starts_with(tablename, 'trading_')) ORDER BY tablename
    """)))
    quote = connection.dialect.identifier_preparer.quote
    result = {}
    for table in tables:
        result[table] = (await connection.execute(text(
            "SELECT count(*), md5(coalesce(string_agg(digest,'' ORDER BY digest),'')) FROM "
            '(SELECT md5(row_to_json(record)::text) AS digest FROM ' + quote(table) + ' record) records'
        ))).one()._asdict()
    return result


async def cleanup(connection, execute=False):
    params = {'sources': list(SOURCES)}
    optional = tuple([table for table in OPTIONAL_AI_TABLES
                     if await connection.scalar(text('SELECT to_regclass(:name)'), {'name': table})])
    ai_tables = (*optional, *AI_TABLES)
    if execute:
        await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        tables = (*ai_tables, 'news_revisions', *SOURCE_TABLES, 'raw_bodies',
                  'news_research_settings', 'risk_settings')
        await connection.execute(text('LOCK TABLE ' + ', '.join(tables) + ' IN SHARE ROW EXCLUSIVE MODE'))
    counts = {table: await connection.scalar(text('SELECT count(*) FROM ' + table)) for table in ai_tables}
    counts.update({table: await connection.scalar(text('SELECT count(*) FROM ' + table +
                   ' WHERE ' + ('code' if table == 'sources' else 'source_code') + ' = ANY(:sources)'), params)
                   for table in SOURCE_TABLES})
    counts['news_revisions'] = await connection.scalar(text('''SELECT count(*) FROM news_revisions
        WHERE news_id IN (SELECT id FROM news_items WHERE source_code = ANY(:sources))'''), params)
    cleanup_indexes = {
        'cleanup_cls_revisions_raw': ('news_revisions', 'raw_response_id'),
        'cleanup_cls_economic_raw': ('economic_revisions', 'raw_response_id'),
        'cleanup_cls_series_raw': ('economic_series_values', 'raw_response_id'),
        'cleanup_cls_response_body': ('raw_responses', 'body_sha256'),
    }
    if execute:
        for name, (table, column) in cleanup_indexes.items():
            await connection.execute(text(f'CREATE INDEX {name} ON {table} ({column})'))
    before = await retained_news(connection)
    protected = await protected_records(connection)
    if execute:
        await connection.execute(text('''CREATE TEMP TABLE IF NOT EXISTS removed_cls_bodies ON COMMIT DROP AS
            SELECT DISTINCT body_sha256 FROM raw_responses WHERE source_code = ANY(:sources)'''), params)
        for table in ('news_research_settings', 'risk_settings'):
            await connection.execute(text('UPDATE ' + table + ' SET enabled = false'))
        await connection.execute(text('UPDATE news_research_settings SET control_revision = control_revision + 1'))
        for table in ai_tables:
            await connection.execute(text('DELETE FROM ' + table))
        await connection.execute(text('''DELETE FROM news_revisions WHERE news_id IN
            (SELECT id FROM news_items WHERE source_code = ANY(:sources))'''), params)
        for table in SOURCE_TABLES:
            await connection.execute(text('DELETE FROM ' + table + ' WHERE ' +
                ('code' if table == 'sources' else 'source_code') + ' = ANY(:sources)'), params)
        result = await connection.execute(text('''DELETE FROM raw_bodies body USING removed_cls_bodies removed
            WHERE body.sha256 = removed.body_sha256 AND NOT EXISTS
            (SELECT 1 FROM raw_responses response WHERE response.body_sha256 = body.sha256)'''))
        counts['unreferenced_raw_bodies'] = result.rowcount
        for name in cleanup_indexes:
            await connection.execute(text('DROP INDEX ' + name))
        if protected != await protected_records(connection):
            raise RuntimeError('保留业务数据校验失败，清理已回滚')
        if before != await retained_news(connection):
            raise RuntimeError('其他来源原文校验失败，清理已回滚')
    return {'executed': execute, 'protected_records': protected, 'counts': counts, 'retained_news': before, 'absent_legacy_tables': sorted(set(OPTIONAL_AI_TABLES) - set(optional))}


async def main(execute=False):
    try:
        async with engine.begin() as connection:
            if not execute:
                await connection.execute(text('SET TRANSACTION READ ONLY'))
            result = await cleanup(connection, execute)
        if execute:
            from alphapulse.core.redis import redis
            async for key in redis.scan_iter(match='ap:risk:*', count=100):
                await redis.delete(key)
            for source in SOURCES:
                async for key in redis.scan_iter(match=f'ap:{source}:*', count=100):
                    await redis.delete(key)
            await redis.hdel('ap:crawler:status', *SOURCES)
            await redis.delete('ap:rl:www.cls.cn')
            await redis.aclose()
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    asyncio.run(main(parser.parse_args().execute))
