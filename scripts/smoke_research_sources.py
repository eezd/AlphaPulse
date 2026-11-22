"""显式隔离数据库/Redis中的研究来源验收；不访问外部网站或模型。"""
import asyncio
import copy
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
import uuid

if os.environ.get('RESEARCH_SOURCES_TEST_ISOLATED') != '1':
    raise RuntimeError('必须指定 RESEARCH_SOURCES_TEST_ISOLATED=1，禁止使用生产资源')
for key in ('DATABASE_URL', 'REDIS_URL'):
    os.environ[key] = os.environ['RESEARCH_SOURCES_TEST_' + key]
os.environ.update(JWT_SECRET='research-test-only-secret-32-characters', ADMIN_USERNAME='research-test',
                  SECRET_ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

import httpx
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select, text
from alphapulse.ai import submission as model
from alphapulse.api.main import app
from alphapulse.briefings import config, contracts, reports, schedule, service
from alphapulse.briefings.models import Analysis, Report
from alphapulse.briefings.worker import candidates
from alphapulse.calendar.service import save_raw
from alphapulse.core.auth import create_token, require_session, revoke_session
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.core.research_sources import SOURCES
from alphapulse.crawler.pipelines import NewsPipeline
from alphapulse.crawler.research_articles import parse_article
from alphapulse.models import NewsItem, NewsRevision, Source


async def main():
    prefix = 'research-' + uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    day = now.astimezone(schedule.SHANGHAI).date() - timedelta(days=2)
    at = datetime.combine(day, datetime.min.time(), schedule.SHANGHAI) + timedelta(hours=1)
    async with SessionLocal() as session:
        assert await session.scalar(text('SELECT version_num FROM alembic_version')) == 'f63456789012'
        sources = list(await session.scalars(select(Source).where(Source.code.in_(SOURCES))))
        assert len(sources) == 6 and all(s.analysis_policy == 'collect_only' for s in sources)
        for row in sources:
            assert row.history_target and row.backfill_hours == 720 and row.interval_seconds == 21600
            assert row.enabled == (row.code in ('citi_insights', 'jpmorgan_research'))
        assert not await session.scalar(select(Source.code).where(Source.code.in_(('wscn_live', 'cls_telegraph'))))
        settings = await config.settings_row(session, lock=True)
        saved = {key: copy.deepcopy(getattr(settings, key)) for key in ('config', 'enabled', 'control_revision', 'enabled_at',
            'history_start', 'history_end', 'history_cursor', 'history_enrolled')}
        settings.config, settings.enabled, settings.control_revision, settings.enabled_at = {'backfill_start': day.isoformat()}, True, 901, now
        session.add(Source(code=prefix, name='原有自动来源', list_url='https://example.invalid', enabled=False, interval_seconds=21600))
        await session.commit()
    spider = N(run=N(items_seen=0, items_new=0, items_revised=0))
    pipeline = NewsPipeline(N(spider=spider))
    async def ingest(code, identifier, body, status=200, title='公开文章', author=None):
        url = SOURCES[code].entry_urls[0] if code in SOURCES else 'https://example.invalid/article'
        if code in SOURCES:
            item = parse_article(code, url, body, {'url': url, 'id': identifier, 'title': title, 'summary': '公开摘要', 'published_at': at}, status)
        else:
            item = dict(external_id=identifier, url=url, title=title, summary='公开摘要', content_html=body,
                        author=author, published_at=at, is_paywalled=False, extra={})
        item.update(source_code=code, tags=[], source_updated_at=None)
        async with SessionLocal() as session:
            item['raw_response_id'] = await save_raw(session, code, url, status, {}, body.encode(), now)
            await session.commit()
        await pipeline.process_item(item)
        async with SessionLocal() as session:
            return await session.scalar(select(NewsItem.id).where(NewsItem.source_code == code, NewsItem.external_id == identifier))
    body = '<div class="bodyContent"><p>研究正文</p><a href="/report.pdf">报告附件</a></div><script type="application/ld+json">{"@type":"Article","author":{"name":"作者_%"}}</script>'
    full_id = await ingest('citi_insights', prefix, body, title='研究全文验收')
    await ingest('citi_insights', prefix, body, title='研究全文验收')
    await ingest('citi_insights', prefix, '<h1>Access denied</h1>', 403, title='受限页面')
    async with SessionLocal() as session:
        news = await session.get(NewsItem, full_id)
        assert news.revision_no == 1 and news.author == '作者_%' and news.title == '研究全文验收'
        assert news.extra['research']['content_status'] == 'full' and news.extra['research']['access_status'] == 'forbidden'
        assert await session.scalar(select(func.count()).select_from(NewsRevision).where(NewsRevision.news_id == full_id)) == 1
    await ingest('citi_insights', prefix, body.replace('研究正文', '研究正文更新'), title='研究全文验收')
    async with SessionLocal() as session:
        assert (await session.get(NewsItem, full_id)).revision_no == 2
    # 同一份真实公开样本通过解析、清洗、证据与修订持久化链路。
    fixture = Path(os.environ.get('RESEARCH_SOURCES_TEST_FIXTURES', Path(__file__).resolve().parents[1] / 'backend/tests/fixtures/research'))
    await ingest('citi_insights', prefix+'-real', (fixture/'citi-detail.html').read_text(), title='Tokenization 2030')
    restricted = [await ingest(code, prefix+'-'+code, '<h1>Forbidden</h1>', 403, title='公开列表元数据 '+SOURCES[code].name) for code in SOURCES]
    automatic = await ingest(prefix, prefix, '<p>研究正文</p>', title='旧来源新闻')
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        await service.enroll(session, settings, now)
        await session.flush()
        assert not await session.scalar(select(Analysis.id).join(NewsItem).where(NewsItem.source_code.in_(SOURCES)))
        old = await session.scalar(select(Analysis).where(Analysis.news_id == automatic))
        assert old is not None
        old_id = old.id
        # 模拟历史审计与手动重试版本，确保版本号不能绕过来源策略。
        fake = []
        for identifier in restricted:
            row = Analysis(news_id=identifier, version_no=2, state='pending', lane='live', effective_at=at, time_basis='published_at')
            session.add(row)
            fake.append(row)
        await session.flush()
        fake_ids = [row.id for row in fake]
        choices = await candidates(session, settings.config)
        assert all(('analysis', identifier, 'live') not in choices for identifier in fake_ids)
        await session.commit()
    calls = []
    async def model_config(*args):
        return N(id=1, protocol='openai', base_url='http://127.0.0.1:1', model='local-fake',
                 reasoning_effort=None, max_tokens=8192, context_window=250000), 'local', {}
    async def complete(provider, system, payload, schema, *, allow_repair=True):
        calls.append(payload['source_code'])
        return contracts.NewsAnalysisDraft(keywords=[{'canonical':'研究', 'aliases':['研究'], 'kind':'topic', 'quote':'研究'}],
            summary='验收摘要', facts=[{'text':'研究正文', 'quote':'研究正文'}], impact='', importance_score=20,
            importance_reason='测试', primary_region={'code':'GLOBAL','name':'全球'}, related_regions=[], assets=[], uncertainties=[], scenarios=[]), {}
    with patch.object(service.config, 'model_config', model_config), patch.object(model, 'complete', complete):
        for identifier in fake_ids + [old_id]:
            await service.process_analysis(identifier, 901)
    assert calls == [prefix], calls
    async with SessionLocal() as session:
        assert (await session.get(Analysis, old_id)).state == 'done'
        for identifier in fake_ids:
            try:
                await service.retry_analysis(session, identifier)
            except ValueError as error:
                assert '仅采集' in str(error)
            else:
                raise AssertionError('重试绕过了来源策略')
        current = await session.get(Analysis, fake_ids[0])
        current.state, current.input, current.output = 'done', {'title':'保留审计'}, {'summary':'保留结果'}
        preserved = copy.deepcopy(current.input), copy.deepcopy(current.output)
        report = await session.scalar(select(Report).where(Report.kind == 'morning', Report.report_date == day))
        assert report is not None
        report.request_id = str(uuid.uuid4())
        assert await service.prepare_period(session, report, now)
        assert report.coverage['total'] == 1 and report.coverage['missing'] == 0, report.coverage
        assert report.input['analysis_ids'] == [old_id]
        assert not await session.scalar(select(NewsItem.id).where(NewsItem.source_code.in_(SOURCES), service.manual_news_scope()))
        # 日报不得借历史分报的文字将仅采集来源重新带入模型上下文。
        for kind in schedule.PERIOD_KINDS:
            left, right, due = schedule.window(kind, day)
            row = report if kind == 'morning' else Report(kind=kind, report_date=day, version_no=99, window_start=left, window_end=right, due_at=due, next_check_at=due)
            row.state, row.input, row.output, row.coverage = 'done', {'analysis_ids':[current.id]}, {'highlight_analysis_ids':[]}, {'total':1,'missing':0}
            if kind != 'morning': session.add(row)
        await session.flush()
        daily = N(report_date=day)
        try:
            await reports.prepare_daily(session, daily, now)
        except ValueError as error:
            assert '未准入' in str(error)
        else:
            raise AssertionError('日报复用了仅采集来源')
        assert (current.input, current.output) == preserved
        await session.rollback()
    token, _ = await create_token('research-test')
    principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Authorization':'Bearer '+token}) as client:
            result = await client.get('/api/news', params={'research_only':'true','author':'作者_%','content_status':'full'})
            assert result.status_code == 200, result.text
            assert [item['id'] for item in result.json()['data']['items']] == [full_id]
            detail = (await client.get(f'/api/news/{full_id}')).json()['data']
            assert detail['research']['attachments'][0]['status'] == 'link_only'
            assert (await client.post(f'/api/briefings/analyses/{fake_ids[0]}/retry')).status_code == 409
            source_rows = (await client.get('/api/crawler/sources')).json()['data']['sources']
            for source in source_rows:
                if source['code'] in SOURCES:
                    assert source['analysis_policy'] == 'collect_only' and source['availability'] == SOURCES[source['code']].availability
            assert (await client.patch('/api/crawler/sources/cls_depth', json={'enabled':True})).status_code == 200
            assert (await client.patch('/api/crawler/sources/goldman_insights', json={'enabled':True})).status_code == 200
            assert (await client.patch('/api/crawler/sources/blackrock_insights', json={'enabled':True})).status_code == 409
    finally:
        await revoke_session(principal)
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            for key, value in saved.items(): setattr(settings, key, value)
            await session.commit()
        await redis.aclose()
        await engine.dispose()
    print('Research sources acceptance passed', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
