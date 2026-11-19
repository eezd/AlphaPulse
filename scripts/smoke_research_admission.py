"""机构准入隔离验收：真实数据库/API、替身模型，不调用外部服务。"""
import asyncio
import copy
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import patch
import uuid

if os.environ.get('RESEARCH_SOURCES_TEST_ISOLATED') != '1':
    raise RuntimeError('必须显式使用独占测试资源')
for key in ('DATABASE_URL', 'REDIS_URL'):
    os.environ[key] = os.environ['RESEARCH_SOURCES_TEST_' + key]

import httpx
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from alphapulse.api.main import app
from alphapulse.ai import submission as model
from alphapulse.briefings import admission, config, contracts, reports, schedule, service
from alphapulse.briefings.models import Analysis, Report, ReportPart
from alphapulse.core.auth import create_token, require_session, revoke_session
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.models import AiProvider, NewsItem, SkillNode, SkillVersion, Source


async def main():
    prefix = 'admission-' + uuid.uuid4().hex[:8]
    token, _ = await create_token(os.environ['ADMIN_USERNAME'])
    principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    now = datetime.now(UTC)
    day = now.astimezone(schedule.SHANGHAI).date() - timedelta(days=4)
    at = datetime.combine(day, datetime.min.time(), schedule.SHANGHAI) + timedelta(hours=1)
    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        settings.enabled = False
        # 前一来源验收中的历史 pending 不影响切换，在途才应阻止。
        provider = AiProvider(name=prefix, protocol='openai', base_url='http://127.0.0.1:1',
            api_key_encrypted=encrypt('fake'), api_key_hint='fake', model='synthetic', context_window=250000, max_tokens=8192)
        session.add(provider)
        await session.flush()
        configuration = {'backfill_start': str(day), 'concurrency': 2}
        versions = []
        for stage in ('analysis', 'period', 'daily'):
            node = SkillNode(name=prefix+stage, kind='skill', scope='news', enabled=True)
            session.add(node)
            await session.flush()
            version = SkillVersion(skill_id=node.id, version_no=1, source='user',
                content=contracts.SKILL_TEMPLATES[stage][1].replace(admission.RESEARCH_MARKER, ''))
            session.add(version)
            await session.flush()
            node.published_version_id = version.id
            versions.append(version.id)
            configuration[stage] = {'provider_id':provider.id, 'skill_id':node.id, 'model':'synthetic', 'max_tokens':8192}
        settings.config = configuration
        await session.commit()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                headers={'Authorization':'Bearer '+token}) as client:
            async def policy(code, value, expected=200):
                response = await client.patch('/api/crawler/sources/'+code, json={'analysis_policy':value})
                assert response.status_code == expected, response.text
            await policy('cls_depth', 'automatic', 422)
            await policy('citi_insights', 'automatic', 409)
            async with SessionLocal() as session:
                for identifier in versions:
                    version = await session.get(SkillVersion, identifier)
                    version.content += '\n' + admission.RESEARCH_MARKER
                await session.commit()
            for code in admission.RESEARCH_CODES:
                await policy(code, 'automatic')
                async with SessionLocal() as session:
                    activated = (await session.get(Source, code)).research_enabled_at
                await policy(code, 'automatic')
                await policy(code, 'collect_only')
                await policy(code, 'automatic')
                async with SessionLocal() as session:
                    assert (await session.get(Source, code)).research_enabled_at == activated
            async with SessionLocal() as session:
                settings = await config.settings_row(session, lock=True)
                settings.enabled = True
                await session.commit()
            await policy('citi_insights', 'collect_only', 409)
            async with SessionLocal() as session:
                settings = await config.settings_row(session, lock=True)
                settings.enabled = False
                # 合成首次启用边界，避免等待现实窗口结束。
                for code in admission.RESEARCH_CODES:
                    (await session.get(Source, code)).research_enabled_at = at - timedelta(minutes=30)
                left, right, due = schedule.window('morning', day)
                report = Report(kind='morning', report_date=day, version_no=10, window_start=left,
                    window_end=right, due_at=due, next_check_at=due, state='processing')
                session.add(report)
                await session.commit()
                report_id = report.id
            await policy('citi_insights', 'collect_only', 409)
            async with SessionLocal() as session:
                report = await session.get(Report, report_id)
                report.state = 'pending'
                part = ReportPart(report_id=report_id, part_key='guard', stage='period_map', state='processing',
                    input_hash='a'*64, control_revision=990, input={}, rules={})
                session.add(part)
                await session.commit()
                part_id = part.id
            await policy('citi_insights', 'collect_only', 409)
            async with SessionLocal() as session:
                await session.delete(await session.get(ReportPart, part_id))
                ids = {}
                specs = [('new', str(day), at, 'full', '研究正文。欧元走弱影响美元收入，HBM4e升级有风险。'),
                    ('before', str(day), at-timedelta(hours=1), 'full', '研究正文'),
                    ('late', str(day-timedelta(days=1)), at, 'full', '研究正文'),
                    ('unknown', None, at, 'full', '研究正文'),
                    ('future', str(day+timedelta(days=1)), at, 'full', '研究正文'),
                    ('invalid', '2026-02-30', at, 'full', '研究正文'),
                    ('excerpt', str(day), at, 'excerpt', '研究正文'),
                    ('image', str(day), at, 'full', ' \n\t')]
                for index, (label, published, seen, status, body) in enumerate(specs):
                    item = NewsItem(source_code=admission.RESEARCH_CODES[index % 3], external_id=prefix+label,
                        url='https://example.invalid/'+label, title='研究准入 '+label, summary='', content_text=body,
                        content_html='<p>'+body+'</p>', content_hash=str(index)*64, first_seen_at=seen, published_at=seen,
                        extra={'published_at_precision':'date', 'published_date':published,
                            'research':{'publisher_code':admission.RESEARCH_CODES[index % 3], 'publisher_name':'合成机构',
                                'bylines':[{'name':'合成作者', 'profile_url':None, 'organization':None}],
                                'canonical_url':'https://example.invalid/'+label, 'original_url':'https://example.invalid/'+label,
                                'language':'zh', 'section':None, 'access_status':'public', 'attachments':[], 'content_status':status}})
                    session.add(item)
                    await session.flush()
                    ids[label] = item.id
                settings = await config.settings_row(session, lock=True)
                settings.enabled, settings.enabled_at, settings.control_revision = True, now, 990
                await service.enroll(session, settings, now)
                await session.commit()
                allowed = set(await session.scalars(select(NewsItem.id).where(NewsItem.id.in_(ids.values()), admission.report_news_allowed())))
                assert allowed == {ids['new']}, allowed
                assert not await session.scalar(select(Analysis.id).where(Analysis.news_id.in_([ids['late'],ids['image'],ids['excerpt']])))
                first = await session.scalar(select(Analysis).where(Analysis.news_id == ids['new']))
                first.state = 'processing'
                settings.enabled = False
                await session.commit()
                first_id = first.id
            await policy('citi_insights', 'collect_only', 409)
            async with SessionLocal() as session:
                (await session.get(Analysis, first_id)).state = 'pending'
                settings = await config.settings_row(session, lock=True)
                settings.enabled = True
                await session.commit()
            calls = []
            async def complete(provider, system, payload, schema, *, allow_repair=True):
                calls.append(copy.deepcopy(payload))
                assert 'importance:publication:v1' in system and 'keywords:v1' in system
                assert 'keyword_candidates' in payload
                aliases = ['研究', '研究X'] if payload['news_id'] == ids['new'] else ['研究']
                return contracts.NewsAnalysisDraft(keywords=[{'canonical':'研究', 'aliases':aliases, 'kind':'topic', 'quote':'研究'}],
                    summary='机构预计变化', facts=[{'text':'研究正文','quote':payload['content_text']}], impact='', importance_score=20,
                    importance_reason='合成', primary_region={'code':'GLOBAL','name':'全球'}, related_regions=[], assets=[], uncertainties=[], scenarios=[]), {}
            with patch.object(model, 'complete', complete):
                async with SessionLocal() as session:
                    tasks = list(await session.scalars(select(Analysis.id).where(Analysis.news_id.in_(ids.values()), Analysis.state == 'pending')))
                for identifier in tasks:
                    await service.process_analysis(identifier, 990)
            assert len(calls) == 5, len(calls)
            async with SessionLocal() as session:
                first = await session.get(Analysis, first_id)
                assert first.state == 'done', first.error
                assert first.input['research_context']['mode'] == 'report'
                assert {word['canonical'] for word in first.keywords} == {'研究', '欧元', '美元', 'HBM4e'}
                assert first.output['keywords'] == first.keywords
                assert first.keywords[0]['aliases'] == ['研究']
                assert first.rules['keyword_lexicon']['sha256'] == first.rules['keyword_processing']['sha256']
                assert first.rules['keyword_processing']['processing_version'] == 'keywords:v3'
                assert first.rules['keyword_processing']['raw_keywords'][0]['aliases'] == ['研究', '研究X']
                assert first.rules['keyword_processing']['decisions'][0] == {
                    'action':'drop_invalid_alias', 'canonical':'研究', 'alias':'研究X',
                    'reason':'not_literal_in_single_field'}
                assert len(first.rules['keyword_processing']['added']) == 3
                source_news = await session.get(NewsItem, ids['new'])
                source_news.extra = {**source_news.extra, 'published_date':'2020-01-01', 'research':{'content_status':'full','publisher_name':'变更机构'}}
                report = await session.get(Report, report_id)
                report.request_id = str(uuid.uuid4())
                assert await service.prepare_period(session, report, now, configuration=configuration)
                assert report.coverage['total'] == 1 and report.coverage['missing'] == 0, report.coverage
                card = report.input['cards'][0]
                assert card['source']['research']['publisher_name'] == '合成机构'
                assert card['source']['research']['published_date'] == str(day)
                assert card['research_context']['mode'] == 'report'
                archive = await session.scalar(select(Analysis).where(Analysis.news_id == ids['before']))
                try:
                    await reports.validate_admission(session, [archive.id])
                except reports.BudgetBlocked:
                    pass
                else:
                    raise AssertionError('历史研究绕过日报/冻结报告校验')
                # 缺少冻结资格的旧研究也不得被提升为当期材料。
                first.input = {'title':'旧审计'}
                try:
                    await reports.validate_admission(session, [first.id])
                except reports.BudgetBlocked:
                    pass
                else:
                    raise AssertionError('旧研究缺失冻结资格仍获准')
                await session.rollback()
            for query in ('USD', '美元', 'HBM4E'):
                response = await client.get('/api/briefings/analyses', params={'q': query})
                assert response.status_code == 200, response.text
                assert first_id in {row['id'] for row in response.json()['data']['items']}, response.text
            # 用真实报表生成入口与替身输出验证机构引用可流入报表，保留供浏览器阅读。
            async with SessionLocal() as session:
                report = await session.get(Report, report_id)
                assert await service.prepare_period(session, report, now, configuration=configuration)
                report.market = []
                await session.commit()
            async def report_complete(provider, system, payload, schema):
                assert payload['cards'][0]['source']['research']['publisher_name'] == '合成机构'
                return schema(title='机构归属合成报告', summary='合成机构的条件观点，非实际发生结果',
                    sections=[{'region':{'code':'GLOBAL','name':'全球'}, 'summary':'仅依据合成材料', 'events':[{
                        'title':'机构研究归属', 'importance_score':20, 'facts':['合成机构／合成作者预计变化，尚待验证'],
                        'source_analysis_ids':[first_id], 'market_changes':[], 'impact':'合成条件分析',
                        'suggestions':[], 'watch_points':[], 'scenarios':[], 'uncertainties':['没有后续实绩']}]}],
                    highlight_analysis_ids=[first_id]), {}
            with patch.object(reports.model, 'complete', report_complete):
                await reports.process_report(report_id, 990)
            async with SessionLocal() as session:
                report = await session.get(Report, report_id)
                assert report.state == 'done', report.error
            for label, expected in [('new','report'),('before','archive'),('late','archive'),('unknown','archive'),('future','archive'),('image','collect_only'),('excerpt','collect_only')]:
                response = await client.get(f'/api/news/{ids[label]}')
                assert response.status_code == 200, response.text
                assert response.json()['data']['analysis_admission']['mode'] == expected
            async with SessionLocal() as session:
                settings = await config.settings_row(session, lock=True)
                settings.enabled = False
                await session.commit()
            await policy('citi_insights', 'collect_only')
            async with SessionLocal() as session:
                try:
                    await service.retry_analysis(session, first_id)
                except ValueError:
                    pass
                else:
                    raise AssertionError('停用后重试绕过来源准入')
            await policy('citi_insights', 'automatic')
        print('Research admission acceptance passed: policy guards, archive/report boundaries, frozen provenance and API', flush=True)
    finally:
        await revoke_session(principal)
        await redis.aclose()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
