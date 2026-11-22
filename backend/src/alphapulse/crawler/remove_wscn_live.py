"""固定退役 wscn_live；调用方必须先停止应用写入并管理事务。"""
from sqlalchemy import text

SOURCE = 'wscn_live'
SOURCE_TABLES = ('news_items', 'raw_responses', 'crawl_logs', 'crawl_runs', 'sources')
LEGACY_TABLES = ('news_ai_article_analyses', 'news_ai_reprocess_items', 'news_research_packages', 'risk_analyses')
PROTECTED_TABLES = ('ai_providers', 'skill_nodes', 'skill_versions', 'chat_sessions', 'chat_messages',
                    'context_versions', 'briefing_settings', 'news_research_settings', 'risk_settings',
                    'briefing_notifications', 'briefing_report_parts', 'economic_releases',
                    'economic_revisions', 'economic_series_values')
CLEANUP_INDEXES = {'cleanup_wscn_revision_raw': ('news_revisions', 'raw_response_id'),
                   'cleanup_wscn_economic_raw': ('economic_revisions', 'raw_response_id'),
                   'cleanup_wscn_series_raw': ('economic_series_values', 'raw_response_id'),
                   'cleanup_wscn_response_body': ('raw_responses', 'body_sha256')}


def references(value, news_ids, analysis_ids):
    if isinstance(value, list):
        return any(references(item, news_ids, analysis_ids) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if key in ('source', 'source_code') and item == SOURCE:
            return True
        if key == 'news_id' and isinstance(item, int) and item in news_ids:
            return True
        if key == 'analysis_id' and isinstance(item, int) and item in analysis_ids:
            return True
        if key in ('news_ids', 'missing_news_ids') and isinstance(item, list) and news_ids.intersection(item):
            return True
        if key == 'analysis_ids' and isinstance(item, list) and analysis_ids.intersection(item):
            return True
        if references(item, news_ids, analysis_ids):
            return True
    return False


async def protected_records(connection, params):
    filters = {table: ('code' if table == 'sources' else 'source_code') + ' <> :source' for table in SOURCE_TABLES}
    filters.update(news_revisions='NOT (news_id = ANY(:news_ids))',
                   news_bookmarks='NOT (news_id = ANY(:news_ids))',
                   briefing_analyses='NOT (news_id = ANY(:news_ids))',
                   briefing_reports='NOT (id = ANY(:report_ids))')
    filters.update({table: 'true' for table in PROTECTED_TABLES})
    result = {}
    # 固定表名/筛选表达式；摘要保留在私有回执，绝不输出业务正文或凭据。
    for table, where in filters.items():
        row = (await connection.execute(text('SELECT count(*) AS count, '
            "md5(coalesce(string_agg(digest,'' ORDER BY digest),'')) AS digest FROM "
            '(SELECT md5(row_to_json(record)::text) AS digest FROM ' + table + ' record WHERE ' + where + ') records'), params)).mappings().one()
        result[table] = dict(row)
    return result


async def cleanup(connection, execute=False) -> dict:
    if execute:
        await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        tables = (*SOURCE_TABLES, 'news_revisions', 'news_bookmarks', 'raw_bodies',
                  'briefing_analyses', 'briefing_reports', *PROTECTED_TABLES, *LEGACY_TABLES)
        await connection.execute(text('LOCK TABLE ' + ', '.join(tables) + ' IN SHARE ROW EXCLUSIVE MODE'))
    params = {'source': SOURCE}
    news_ids = set(await connection.scalars(text('SELECT id FROM news_items WHERE source_code=:source'), params))
    params['news_ids'] = sorted(news_ids)
    analysis_ids = set(await connection.scalars(text('SELECT id FROM briefing_analyses WHERE news_id=ANY(:news_ids)'), params))
    reports = list((await connection.execute(text('SELECT * FROM briefing_reports ORDER BY id'))).mappings())
    # 来源当前时间和已冻结分析时间都参与受影响窗口判断。
    windows = set(await connection.scalars(text('''SELECT report.id FROM briefing_reports report WHERE EXISTS (
        SELECT 1 FROM news_items news WHERE news.source_code=:source AND
        CASE WHEN coalesce(news.extra->>'published_at_precision',news.extra->>'time_precision') IN ('date','day','month','unknown')
             OR news.extra->>'published_at_unreliable' = 'true' THEN news.first_seen_at
             ELSE coalesce(news.published_at,news.first_seen_at) END >= report.window_start AND
        CASE WHEN coalesce(news.extra->>'published_at_precision',news.extra->>'time_precision') IN ('date','day','month','unknown')
             OR news.extra->>'published_at_unreliable' = 'true' THEN news.first_seen_at
             ELSE coalesce(news.published_at,news.first_seen_at) END < report.window_end)
        OR EXISTS (SELECT 1 FROM briefing_analyses analysis WHERE analysis.news_id=ANY(:news_ids)
        AND analysis.effective_at>=report.window_start AND analysis.effective_at<report.window_end)'''), params))
    affected = [row for row in reports if row['id'] in windows or
                references(row['input'], news_ids, analysis_ids) or references(row['coverage'], news_ids, analysis_ids)]
    params['report_ids'] = [row['id'] for row in affected]
    parts = await connection.scalar(text('SELECT count(*) FROM briefing_report_parts WHERE report_id=ANY(:report_ids)'), params)
    executed_reports = [row['id'] for row in affected if row['started_at'] is not None or row['output'] is not None]
    if parts or executed_reports:
        raise RuntimeError(f'受影响报告已有执行审计，拒绝清理：reports={executed_reports}, parts={parts}')
    for table in ('briefing_analyses', 'briefing_reports', 'briefing_report_parts'):
        if await connection.scalar(text('SELECT count(*) FROM ' + table + " WHERE state='processing'")):
            raise RuntimeError('存在处理中的任务，必须先排空：' + table)
    for table in LEGACY_TABLES:
        if await connection.scalar(text('SELECT count(*) FROM ' + table + ' WHERE news_id=ANY(:news_ids)'), params):
            raise RuntimeError('目标新闻仍有旧AI依赖，拒绝扩大删除范围：' + table)
    for table, retained in (('news_revisions', 'AND NOT (news_id=ANY(:news_ids))'),
                            ('economic_revisions', ''), ('economic_series_values', '')):
        if await connection.scalar(text('SELECT count(*) FROM ' + table + ' WHERE raw_response_id IN '
                '(SELECT id FROM raw_responses WHERE source_code=:source) ' + retained), params):
            raise RuntimeError('目标原始响应仍被保留数据引用：' + table)
    counts = {table: await connection.scalar(text('SELECT count(*) FROM ' + table + ' WHERE ' +
              ('code' if table == 'sources' else 'source_code') + '=:source'), params) for table in SOURCE_TABLES}
    for table in ('news_revisions', 'news_bookmarks', 'briefing_analyses'):
        counts[table] = await connection.scalar(text('SELECT count(*) FROM ' + table + ' WHERE news_id=ANY(:news_ids)'), params)
    params['body_hashes'] = list(await connection.scalars(text('SELECT DISTINCT body_sha256 FROM raw_responses WHERE source_code=:source'), params))
    before = await protected_records(connection, params)
    if execute:
        for name, (table, column) in CLEANUP_INDEXES.items():
            await connection.execute(text(f'CREATE INDEX {name} ON {table} ({column})'))
        # 只有尚未发送的资料可以失效；报告身份、窗口、请求UUID及排程保留。
        await connection.execute(text('''UPDATE briefing_reports SET state='pending', input=NULL, coverage=NULL,
            market=NULL,rules=NULL,error=NULL,first_check_at=NULL,cutoff_at=NULL,finished_at=NULL,
            control_revision=NULL,next_check_at=due_at WHERE id=ANY(:report_ids)'''), params)
        for table in ('briefing_analyses', 'news_bookmarks', 'news_revisions'):
            await connection.execute(text('DELETE FROM ' + table + ' WHERE news_id=ANY(:news_ids)'), params)
        for table in SOURCE_TABLES:
            await connection.execute(text('DELETE FROM ' + table + ' WHERE ' +
                ('code' if table == 'sources' else 'source_code') + '=:source'), params)
        result = await connection.execute(text('''DELETE FROM raw_bodies body WHERE body.sha256=ANY(:body_hashes)
            AND NOT EXISTS (SELECT 1 FROM raw_responses response WHERE response.body_sha256=body.sha256)'''), params)
        counts['unreferenced_raw_bodies'] = result.rowcount
        for name in CLEANUP_INDEXES:
            await connection.execute(text('DROP INDEX ' + name))
        if before != await protected_records(connection, params):
            raise RuntimeError('保留数据校验失败，必须回滚清理事务')
    return {'executed': execute, 'counts': counts, 'affected_report_ids': params['report_ids'], 'protected_records': before}
