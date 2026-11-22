"""仅在独占 CI 库验证快讯定向退役；--upgrade 覆盖带数据的旧 head。"""
import asyncio
from datetime import UTC, datetime, timedelta
import subprocess
import sys

from sqlalchemy import insert, text

from alphapulse.core.config import get_settings
from alphapulse.core.db import engine
from alphapulse.crawler.remove_wscn_live import cleanup
from alphapulse.briefings.models import Analysis, Report, ReportPart
from alphapulse.models import NewsBookmark, NewsItem, NewsRevision, RawBody, RawResponse, Source

IDENTITY = 8000001
AT = datetime(1999, 1, 1, tzinfo=UTC)


def migrate(revision, succeeds=True):
    result = subprocess.run(['alembic', 'upgrade', revision], capture_output=True, text=True)
    assert (result.returncode == 0) == succeeds, result.stderr
    if not succeeds:
        assert 'ForeignKeyViolationError' in result.stderr


async def fixture(connection, *, legacy=False):
    if not legacy:
        await connection.execute(insert(Source).values(code='wscn_live', name='CI flash', list_url='https://example.invalid', interval_seconds=120))
    for offset, source in ((0, 'wscn_live'), (1, 'wscn_news')):
        await connection.execute(insert(NewsItem).values(id=IDENTITY+offset, source_code=source, external_id='removal-ci',
            url='https://example.invalid', title='CI retained' if offset else 'CI deleted', published_at=AT,
            first_seen_at=AT, content_hash=str(IDENTITY+offset)))
        await connection.execute(insert(Analysis).values(id=IDENTITY+offset, news_id=IDENTITY+offset, state='done',
            effective_at=AT, time_basis='published_at', input={'news_id': IDENTITY+offset}, output={'retained': offset}))
    for digest in ('a'*64, 'b'*64, 'c'*64):
        await connection.execute(insert(RawBody).values(sha256=digest, body=b'ci'))
    for offset, source, digest in ((0, 'wscn_live', 'a'*64), (1, 'wscn_news', 'a'*64), (2, 'wscn_live', 'b'*64)):
        await connection.execute(insert(RawResponse).values(id=IDENTITY+offset, source_code=source, url='https://example.invalid',
            method='GET', status=200, headers={}, body_sha256=digest))
    await connection.execute(insert(NewsRevision).values(id=IDENTITY, news_id=IDENTITY, revision_no=1,
        title='CI deleted', summary='', content_html='', content_hash=str(IDENTITY), raw_response_id=IDENTITY))
    await connection.execute(insert(NewsBookmark).values(username='ci', news_id=IDENTITY))
    for offset in range(3):
        start = AT + timedelta(days=offset)
        await connection.execute(insert(Report).values(id=IDENTITY+offset, kind='morning', report_date=start.date(),
            state='pending', window_start=start, window_end=start+timedelta(hours=9), due_at=start+timedelta(hours=9, minutes=5),
            next_check_at=start, input={'news_ids': [IDENTITY if offset < 2 else IDENTITY+1]},
            coverage={'missing_news_ids': [IDENTITY] if offset < 2 else []}, rules={'ci': True}))


async def expect_refusal(connection, message):
    try:
        await cleanup(connection, True)
    except RuntimeError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError('清理应拒绝不满足条件的数据')


async def main():
    database = get_settings().database_url
    assert database.startswith('postgresql+asyncpg://ci:ci-only@ap-ci-') and database.split('?', 1)[0].endswith('/ci')
    upgrade = '--upgrade' in sys.argv
    try:
        if upgrade:
            migrate('a18901234567')
            async with engine.begin() as connection:
                await fixture(connection, legacy=True)
            migrate('head', succeeds=False)
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if not upgrade:
                    assert not await connection.scalar(text("SELECT count(*) FROM sources WHERE code='wscn_live'"))
                    await fixture(connection)
                preview = await cleanup(connection)
                assert not preview['executed'] and preview['counts']['news_items'] == 1
                assert preview['affected_report_ids'] == [IDENTITY, IDENTITY+1]
                await connection.execute(text('UPDATE briefing_reports SET started_at=:at WHERE id=:id'), {'at':AT,'id':IDENTITY})
                await expect_refusal(connection, '执行审计')
                await connection.execute(text('UPDATE briefing_reports SET started_at=NULL WHERE id=:id'), {'id':IDENTITY})
                await connection.execute(insert(ReportPart).values(id=IDENTITY, report_id=IDENTITY, part_key='ci', input_hash='x',
                    stage='ci', state='done', control_revision=1, input={}, rules={}))
                await expect_refusal(connection, '执行审计')
                await connection.execute(text('DELETE FROM briefing_report_parts WHERE id=:id'), {'id':IDENTITY})
                await connection.execute(insert(NewsRevision).values(id=IDENTITY+1, news_id=IDENTITY+1, revision_no=1,
                    title='retained', summary='', content_html='', content_hash='ci', raw_response_id=IDENTITY))
                await expect_refusal(connection, '保留数据引用')
                await connection.execute(text('DELETE FROM news_revisions WHERE id=:id'), {'id':IDENTITY+1})
                savepoint = await connection.begin_nested()
                await cleanup(connection, True)
                await savepoint.rollback()
                assert await connection.scalar(text('SELECT count(*) FROM news_items WHERE id=:id'), {'id':IDENTITY}) == 1
                result = await cleanup(connection, True)
                assert result['counts']['briefing_analyses'] == 1
                assert result['counts']['unreferenced_raw_bodies'] == 1
                assert await connection.scalar(text('SELECT count(*) FROM news_items WHERE id=:id'), {'id':IDENTITY+1}) == 1
                assert await connection.scalar(text('SELECT count(*) FROM briefing_analyses WHERE id=:id'), {'id':IDENTITY+1}) == 1
                assert await connection.scalar(text("SELECT count(*) FROM raw_bodies WHERE sha256 IN (:shared,:unrelated)"),
                    {'shared':'a'*64, 'unrelated':'c'*64}) == 2
                for identity in (IDENTITY, IDENTITY+1):
                    row = (await connection.execute(text('SELECT input,coverage,rules,state FROM briefing_reports WHERE id=:id'), {'id':identity})).one()
                    assert tuple(row) == (None, None, None, 'pending')
                assert await connection.scalar(text('SELECT input FROM briefing_reports WHERE id=:id'), {'id':IDENTITY+2}) == {'news_ids':[IDENTITY+1]}
                repeat = await cleanup(connection, True)
                assert not repeat['affected_report_ids'] and repeat['counts']['sources'] == 0
                if upgrade:
                    await transaction.commit()
                print('WSCN live removal acceptance passed', flush=True)
            finally:
                if transaction.is_active:
                    await transaction.rollback()
        if upgrade:
            migrate('head')
            async with engine.connect() as connection:
                assert await connection.scalar(text('SELECT version_num FROM alembic_version')) == 'f63456789012'
    finally:
        await engine.dispose()


asyncio.run(main())
