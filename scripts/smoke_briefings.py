"""仅在显式指定的一次性 PostgreSQL/Redis 中验证新报表；模型与 TG 均为本地合成边界。"""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as N
import uuid

# 不读取项目 .env，不回退到正在运行的实例。
for name in ('DATABASE_URL', 'REDIS_URL'):
    os.environ[name] = os.environ['BRIEFINGS_TEST_' + name]
if os.environ.get('BRIEFINGS_TEST_ISOLATED') != '1':
    raise RuntimeError('必须显式确认 BRIEFINGS_TEST_ISOLATED=1；禁止指向生产资源')
os.environ.update(JWT_SECRET='briefing-test-only-secret-32-characters', ADMIN_USERNAME='briefing-test',
                  SECRET_ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

import httpx
from sqlalchemy import event, func, select, text
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.models.news import NewsItem, Source
from alphapulse.models.ai import AiProvider, SkillNode, SkillVersion
from alphapulse.briefings import api, config, contracts, notifications, queue, reports, schedule, service
from alphapulse.briefings.models import Analysis, Notification, Report, ReportPart
from alphapulse.ai import submission as model, progress


async def configuration_acceptance(day, configuration, now):
    """真实事务验证初始化、配置保存和回补边界；不发送模型请求。"""
    from alphapulse.api.main import app
    from alphapulse.core.auth import create_token, require_session, revoke_session
    from alphapulse.briefings.worker import candidates
    from fastapi.security import HTTPAuthorizationCredentials
    from unittest.mock import patch

    token, _ = await create_token('briefing-test')
    principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        endpoint = '/api/briefings/skill-templates/initialize'
        assert (await client.post(endpoint, json={'base_revision': 0})).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + token
        current = (await client.get('/api/briefings/settings')).json()['data']
        revision = current['control_revision']
        assert (await client.post(endpoint, json={'base_revision': revision - 1})).status_code == 409
        # 现有自定义已发布内容可复用，重复初始化不追加版本。
        async with SessionLocal() as session:
            before = {item.id: (item.published_version_id, item.enabled) for item in await session.scalars(select(SkillNode))}
            stored_versions = {item.id: item.content for item in await session.scalars(select(SkillVersion))}
            versions = len(stored_versions)
        response = await client.post(endpoint, json={'base_revision': revision})
        assert response.status_code == 200, response.text
        selected = {item['stage']: item['skill_id'] for item in response.json()['data']}
        assert selected == {stage: configuration[stage]['skill_id'] for stage in ('analysis', 'period', 'daily')}
        async with SessionLocal() as session:
            assert versions == await session.scalar(select(func.count()).select_from(SkillVersion))
            assert stored_versions == {item.id: item.content for item in await session.scalars(select(SkillVersion))}
            assert before == {item.id: (item.published_version_id, item.enabled) for item in await session.scalars(select(SkillNode))}
            # 模拟没有有效模板的首次初始化；旧版本仍完整保留。
            for identifier in selected.values():
                (await session.get(SkillNode, identifier)).enabled = False
            await session.commit()
        responses = await asyncio.gather(*(client.post(endpoint, json={'base_revision': revision}) for _ in range(2)))
        assert all(item.status_code == 200 for item in responses), [item.text for item in responses]
        assert responses[0].json()['data'] == responses[1].json()['data']
        selected = {item['stage']: item['skill_id'] for item in responses[0].json()['data']}
        unchanged = (await client.get('/api/briefings/settings')).json()['data']
        assert unchanged['config'] == current['config'] and unchanged['control_revision'] == revision
        assert not unchanged['enabled']
        async with SessionLocal() as session:
            assert len(before) + 3 == await session.scalar(select(func.count()).select_from(SkillNode))
            assert versions + 3 == await session.scalar(select(func.count()).select_from(SkillVersion))
            # 若同名草稿占位，整个初始化回滚，不能留下前面阶段的半成品。
            analysis = await session.get(SkillNode, selected['analysis'])
            period = await session.get(SkillNode, selected['period'])
            original_name, original_version = analysis.name, period.published_version_id
            analysis.name, analysis.enabled, period.published_version_id = original_name + '-disabled', False, None
            await session.commit()
        conflict = await client.post(endpoint, json={'base_revision': revision})
        assert conflict.status_code == 409, conflict.text
        async with SessionLocal() as session:
            assert len(before) + 3 == await session.scalar(select(func.count()).select_from(SkillNode))
            assert versions + 3 == await session.scalar(select(func.count()).select_from(SkillVersion))
            analysis = await session.get(SkillNode, selected['analysis'])
            period = await session.get(SkillNode, selected['period'])
            analysis.name, analysis.enabled, period.published_version_id = original_name, True, original_version
            await session.commit()
        earlier = day - timedelta(days=2)
        body = {**configuration, 'base_revision': revision, 'backfill_start': str(earlier)}
        for stage, identifier in selected.items():
            body[stage] = {**configuration[stage], 'skill_id': identifier}
        invalid = {**body, 'backfill_start': str(datetime.now(schedule.SHANGHAI).date() + timedelta(days=1))}
        assert (await client.put('/api/briefings/settings', json=invalid)).status_code == 422
        response = await client.put('/api/briefings/settings', json=body)
        assert response.status_code == 200, response.text
        saved = response.json()['data']
        revision = saved['control_revision']
        assert not saved['enabled'] and saved['config']['backfill_start'] == str(earlier)
        assert saved['history_cursor'] is None and not saved['history_enrolled']
        assert (await client.delete(f"/api/skills/nodes/{selected['analysis']}")).status_code == 409
        outside = earlier - timedelta(days=1)
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            await service.enroll(session, settings, now)
            assert settings.history_start == earlier
            for kind in schedule.KINDS:
                assert await session.scalar(select(Report.id).where(Report.kind == kind, Report.report_date == earlier))
            # 范围外自动任务既不能准备资料、入池，也不能绕过 claim 检查发请求；手动任务可继续。
            rows = []
            for kind in ('morning', 'evening'):
                left, right, due = schedule.window(kind, outside)
                row = Report(kind=kind, report_date=outside, version_no=1, lane='historical_backfill',
                    window_start=left, window_end=right, due_at=due, next_check_at=due,
                    request_id=str(uuid.uuid4()) if kind == 'evening' else None)
                session.add(row)
                await session.flush()
                rows.append(row.id)
            await reports.prepare_due(session, settings, now)
            assert (await session.get(Report, rows[0])).input is None
            assert (await session.get(Report, rows[1])).input is not None
            actions = await candidates(session, settings.config)
            assert ('report', rows[0], 'historical_backfill') not in actions
            assert ('report', rows[1], 'historical_backfill') in actions
            settings.enabled = True
            await session.commit()
        with patch.object(config, 'model_config') as forbidden:
            await reports.process_report(rows[0], revision)
            forbidden.assert_not_called()
        async with SessionLocal() as session:
            assert (await session.get(Report, rows[0])).state == 'pending'
        body.update(base_revision=revision, backfill_start=str(outside))
        expanded = await client.put('/api/briefings/settings', json=body)
        assert expanded.status_code == 200, expanded.text
        assert expanded.json()['data']['history_cursor'] is None
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            await service.enroll(session, settings, now)
            assert settings.history_start == outside
            for kind in schedule.KINDS:
                assert await session.scalar(select(Report.id).where(Report.kind == kind, Report.report_date == outside))
            await session.commit()
        coverage = (await client.get('/api/briefings/coverage')).json()['data']
        assert coverage['historical_windows_expected'] > 0
    await revoke_session(principal)


async def global_backfill_acceptance(day, now):
    """统一日期约束真实新闻入队、候选、claim 和统计，验证范围扩缩及手动恢复。"""
    from alphapulse.api.main import app
    from alphapulse.core.auth import create_token, require_session, revoke_session
    from alphapulse.briefings.worker import candidates
    from fastapi.security import HTTPAuthorizationCredentials
    from unittest.mock import patch

    cutoff = schedule.backfill_cutoff({'backfill_start': str(day)})
    before, after = cutoff - timedelta(seconds=1), cutoff + timedelta(seconds=1)
    prefix = 'global-date-' + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        source = await session.scalar(select(NewsItem.source_code).limit(1))
        news = {}
        for name, published, seen, extra in (
            ('outside', before, before, {}), ('boundary', cutoff, cutoff, {}),
            ('unreliable', before, after, {'published_at_unreliable': True}),
            ('coarse', after, before, {'published_at_precision': 'date'}),
            ('moved_out', before, before, {}), ('moved_in', after, after, {}),
        ):
            item = NewsItem(source_code=source, external_id=prefix + name, url='https://example.invalid/' + name,
                title='日期边界新闻', summary='', content_text='合成边界证据', content_hash='f' * 64,
                published_at=published, first_seen_at=seen, extra=extra)
            session.add(item)
            await session.flush()
            news[name] = item.id
        pending = {}
        for name, frozen in (('outside', before), ('moved_out', after), ('moved_in', before)):
            row = Analysis(news_id=news[name], version_no=1, state='pending', lane='historical_backfill',
                           effective_at=frozen, time_basis='published_at')
            session.add(row)
            await session.flush()
            pending[name] = row.id
        # 实际持久化旧配置，只读接口应统一展示而不改写它。
        settings = await config.settings_row(session, lock=True)
        legacy = {key: value for key, value in settings.config.items() if key != 'backfill_start'}
        legacy.update(period_backfill_start=str(day + timedelta(days=1)), daily_backfill_start=str(day))
        settings.config = legacy
        await session.commit()
    token, _ = await create_token('briefing-test')
    principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer ' + token}) as client:
            current = (await client.get('/api/briefings/settings')).json()['data']
            assert current['config']['backfill_start'] == str(day)
            assert not {'period_backfill_start', 'daily_backfill_start'} & current['config'].keys()
            async with SessionLocal() as session:
                assert (await config.settings_row(session)).config == legacy
            body = {**current['config'], 'base_revision': current['control_revision']}
            saved = await client.put('/api/briefings/settings', json=body)
            assert saved.status_code == 200, saved.text
            revision = saved.json()['data']['control_revision']
            async with SessionLocal() as session:
                settings = await config.settings_row(session, lock=True)
                await service.enroll(session, settings, now)
                assert settings.history_start == day
                for kind in schedule.KINDS:
                    assert await session.scalar(select(Report.id).where(Report.kind == kind, Report.report_date == day))
                enrolled = set(await session.scalars(select(Analysis.news_id)))
                assert {news[name] for name in ('boundary', 'unreliable', 'moved_in')} <= enrolled
                assert news['coarse'] not in enrolled
                actions = await candidates(session, settings.config)
                assert ('analysis', pending['moved_in'], 'historical_backfill') in actions
                for name in ('outside', 'moved_out'):
                    assert ('analysis', pending[name], 'historical_backfill') not in actions
                expected_total = await session.scalar(select(func.count()).select_from(NewsItem)
                                                       .where(service.news_scope(settings.config)))
                settings.enabled = True
                await session.commit()
            coverage = (await client.get('/api/briefings/coverage')).json()['data']
            assert coverage['news_total'] == expected_total and coverage['not_enrolled'] == 0
            with patch.object(config, 'model_config') as forbidden:
                for name in ('outside', 'moved_out'):
                    await service.process_analysis(pending[name], revision)
                forbidden.assert_not_called()
            async with SessionLocal() as session:
                assert (await session.get(Analysis, pending['outside'])).state == 'pending'
                # 显式手动重试接替范围外首版，不再返回永远不能执行的首版ID。
                retry = await service.retry_analysis(session, pending['moved_out'])
                assert retry != pending['moved_out']
                assert (await session.get(Analysis, pending['moved_out'])).state == 'interrupted'
                assert (await session.get(Analysis, retry)).version_no == 2
                assert ('analysis', retry, 'live') in await candidates(session, body)
                assert await service.retry_analysis(session, retry) == retry
                await session.commit()
            # 扩大日期重新扫描未入队新闻，已有范围外 pending 恢复候选且不重建版本。
            body.update(base_revision=revision, backfill_start=str(day - timedelta(days=1)))
            saved = await client.put('/api/briefings/settings', json=body)
            assert saved.status_code == 200, saved.text
            assert saved.json()['data']['history_cursor'] is None
            async with SessionLocal() as session:
                settings = await config.settings_row(session, lock=True)
                await service.enroll(session, settings, now)
                assert settings.history_start == day - timedelta(days=1)
                assert await session.scalar(select(Analysis.id).where(Analysis.news_id == news['coarse']))
                assert ('analysis', pending['outside'], 'historical_backfill') in await candidates(session, settings.config)
                assert await session.scalar(select(func.count()).select_from(Analysis)
                                            .where(Analysis.news_id == news['outside'])) == 1
                await session.commit()
            assert not saved.json()['data']['enabled']
    finally:
        await revoke_session(principal)


async def legacy_cursor_acceptance(day, now):
    """旧配置直接启用与已启用升级均重扫新增日报，且只重置一次游标。"""
    for offset, direct_enable in ((70, True), (90, False)):
        start = day - timedelta(days=offset)
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            selected = {key: value for key, value in settings.config.items() if key != 'backfill_start'}
            selected.update(period_backfill_start=str(start), daily_backfill_start=str(start + timedelta(days=3)))
            settings.config, settings.enabled = selected, not direct_enable
            settings.history_start, settings.history_end = start, day
            settings.history_cursor, settings.history_enrolled = day + timedelta(days=1), True
            await session.commit()
            if direct_enable:
                result = await api.enable(api.RevisionInput(base_revision=settings.control_revision), session)
                assert result.data['config']['backfill_start'] == str(start)
                assert result.data['history_cursor'] is None
            settings = await config.settings_row(session, lock=True)
            await service.enroll(session, settings, now)
            assert settings.config['backfill_start'] == str(start)
            assert not {'period_backfill_start', 'daily_backfill_start'} & settings.config.keys()
            assert settings.history_start == start
            for index in range(3):
                assert await session.scalar(select(Report.id).where(Report.kind == 'daily',
                    Report.report_date == start + timedelta(days=index)))
            cursor = settings.history_cursor
            await service.enroll(session, settings, now)
            assert settings.history_cursor == cursor + timedelta(days=7)
            await session.commit()


async def manual_analysis_acceptance(day, now, complete):
    """范围外非空日报复用成功版本、准入首版并经真实候选/claim完成，不扩大自动范围。"""
    from alphapulse.briefings.worker import candidates
    from unittest.mock import patch

    target = day - timedelta(days=110)
    prefix = 'manual-analysis-' + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        settings.config = {**settings.config, 'backfill_start': str(day)}
        settings.enabled, settings.enabled_at = True, schedule.window('morning', target - timedelta(days=1))[0]
        revision = settings.control_revision
        source = await session.scalar(select(NewsItem.source_code).limit(1))
        news = []
        for index, hour in enumerate((1, 2, 12, 21, 25)):
            at = schedule.window('morning', target)[0] + timedelta(hours=hour)
            item = NewsItem(source_code=source, external_id=prefix + str(index),
                url='https://example.invalid/' + prefix + str(index), title='ETH 上涨5%', summary='',
                content_text='ETH 上涨5%，尚待核实。', content_hash='b' * 64, published_at=at, first_seen_at=at)
            session.add(item)
            await session.flush()
            news.append(item)
        pending = Analysis(news_id=news[1].id, version_no=1, state='pending', lane='historical_backfill',
            effective_at=news[1].published_at, time_basis='published_at')
        payload = service.article_snapshot(news[2])
        output, _ = await complete(None, '', payload, contracts.NewsAnalysisResult)
        done = Analysis(news_id=news[2].id, version_no=1, state='done', lane='historical_backfill',
            effective_at=news[2].published_at, time_basis='published_at', input=payload,
            output=output.model_dump(), importance_stars=4)
        session.add_all([pending, done])
        await session.flush()
        assert not await session.scalar(select(Analysis.id).where(Analysis.id == pending.id,
                                                                  service.analysis_scope(settings.config)))
        identifier = str(uuid.uuid4())
        daily = await reports.request_report(session, 'daily', target, identifier)
        assert daily.lane == 'live'
        assert (await reports.request_report(session, 'daily', target, identifier)).id == daily.id
        dependencies = list(await session.scalars(select(Report).where(Report.report_date == target,
                                                                      Report.kind.in_(schedule.PERIOD_KINDS))))
        assert len(dependencies) == 3 and all(row.request_id is not None for row in dependencies)
        # 即使超过一小时，首次分析/依赖尚未完成也不能先耗尽缺失时钟。
        check = now + timedelta(hours=2)
        await reports.prepare_due(session, settings, check)
        morning = next(row for row in dependencies if row.kind == 'morning')
        assert morning.state == daily.state == 'waiting_backfill'
        assert morning.first_check_at is daily.first_check_at is None
        await service.enroll(session, settings, check)
        assert settings.config['backfill_start'] == str(day)
        selected = list(await session.scalars(select(Analysis).where(Analysis.news_id.in_([item.id for item in news]))))
        assert {row.news_id for row in selected} == {item.id for item in news[:4]}
        assert all(row.version_no == 1 for row in selected)
        actions = await candidates(session, settings.config)
        assert ('analysis', pending.id, 'historical_backfill') in actions
        queued = [row.id for row in selected if row.state == 'pending']
        assert len(queued) == 3
        await session.commit()
    with patch.object(model, 'complete', side_effect=complete):
        for identifier in queued:
            await service.process_analysis(identifier, revision)
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            assert all(row.state == 'done' for row in await session.scalars(select(Analysis).where(Analysis.id.in_(queued))))
            assert (await session.get(Analysis, done.id)).input == payload
            await reports.prepare_due(session, settings, check + timedelta(minutes=1))
            for row in dependencies:
                current = await session.get(Report, row.id)
                assert current.input is not None and current.state == 'pending'
            await session.commit()
        for row in dependencies:
            await reports.process_report(row.id, revision)
        async with SessionLocal() as session:
            settings = await config.settings_row(session, lock=True)
            await reports.prepare_due(session, settings, check + timedelta(minutes=2))
            current = await session.get(Report, daily.id)
            assert current.state == 'pending' and len(current.input['dependencies']) == 3
            await session.commit()
        await reports.process_report(daily.id, revision)
    async with SessionLocal() as session:
        assert (await session.get(Report, daily.id)).state == 'done'
        assert await session.scalar(select(func.count()).select_from(Analysis)
                                    .where(Analysis.news_id.in_([item.id for item in news]))) == 4
        settings = await config.settings_row(session, lock=True)
        # 已完成补报不继续准入新到新闻；历史失败也不能因补报被隐式重试。
        late = NewsItem(source_code=source, external_id=prefix + '-late', url='https://example.invalid/late',
            title='ETH 上涨5%', summary='', content_text='ETH 上涨5%', content_hash='c' * 64,
            published_at=news[0].published_at, first_seen_at=now)
        session.add(late)
        await session.flush()
        failed_day = target - timedelta(days=2)
        failed_news = await session.get(NewsItem, news[4].id)
        failed_news.published_at = schedule.window('morning', failed_day)[0] + timedelta(hours=1)
        failed = Analysis(news_id=failed_news.id, version_no=1, state='failed', lane='historical_backfill',
                          effective_at=failed_news.published_at, time_basis='published_at', error='合成旧失败')
        session.add(failed)
        await session.flush()
        blocked = await reports.request_report(session, 'morning', failed_day, str(uuid.uuid4()))
        await service.enroll(session, settings, check)
        assert not await session.scalar(select(Analysis.id).where(Analysis.news_id == late.id))
        assert await session.scalar(select(func.count()).select_from(Analysis).where(Analysis.news_id == news[4].id)) == 1
        assert not await service.prepare_period(session, blocked, check, configuration=settings.config)
        assert blocked.state == 'blocked' and failed.state == 'failed'
        await session.commit()


async def manual_backfill_acceptance(day, now):
    """真实活动窗口约束下验证手动接替、日报依赖和幂等事务。"""
    from alphapulse.api.main import app
    from alphapulse.core.auth import create_token, require_session, revoke_session
    from alphapulse.briefings.worker import candidates
    from fastapi.security import HTTPAuthorizationCredentials
    from unittest.mock import patch

    async def add_report(session, kind, at, state='pending', request_id=None):
        left, right, due = schedule.window(kind, at)
        row = Report(kind=kind, report_date=at, version_no=1, state=state, lane='historical_backfill',
            window_start=left, window_end=right, due_at=due, next_check_at=now, request_id=request_id,
            first_check_at=now - timedelta(minutes=30), input={'analysis_ids': [], 'cards': []})
        session.add(row)
        await session.flush()
        return row

    async def rejected(session, kind, at, identifier, message, regenerate=False):
        try:
            await reports.request_report(session, kind, at, identifier, regenerate)
        except ValueError as error:
            assert message in str(error), str(error)
        else:
            raise AssertionError('应拒绝冲突的手动请求')

    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        settings.config = {**settings.config, 'backfill_start': str(day)}
        settings.enabled = True
        revision = settings.control_revision
        for index, state in enumerate(('pending', 'waiting_backfill', 'waiting_coverage')):
            at = day - timedelta(days=20 + index)
            old = await add_report(session, 'morning', at, state)
            frozen = dict(old.input)
            part = ReportPart(report_id=old.id, part_key='saved', stage='period_map', state='done',
                              control_revision=revision, rules={'audit': 'old rules'},
                              input_hash='1' * 64, input={'audit': 'old input'}, output={'audit': 'old output'})
            session.add(part)
            await session.flush()
            identifier = str(uuid.uuid4())
            newer = await reports.request_report(session, 'morning', at, identifier)
            assert newer.id != old.id and newer.version_no == 2 and newer.first_check_at >= now
            assert old.state == 'interrupted' and old.finished_at is not None and old.request_id is None
            await session.refresh(old)
            await session.refresh(part)
            assert old.input == frozen and part.output == {'audit': 'old output'} and part.state == 'done'
            assert (await reports.request_report(session, 'morning', at, identifier)).id == newer.id
            await rejected(session, 'morning', at, identifier, '请求ID已用于不同', True)
            await rejected(session, 'morning', at, str(uuid.uuid4()), '已有进行中的报告')
        # 正常范围内和已领取的自动请求不允许被手动请求中止。
        for at, state in ((day - timedelta(days=50), 'pending'), (day - timedelta(days=25), 'processing')):
            cutoff = at if state == 'pending' else day
            settings.config = {**settings.config, 'backfill_start': str(cutoff)}
            active = await add_report(session, 'morning', at, state)
            await rejected(session, 'morning', at, str(uuid.uuid4()), '已有进行中的报告')
            assert active.state == state and active.request_id is None
        settings.config = {**settings.config, 'backfill_start': str(day)}
        await rejected(session, 'daily', datetime.now(schedule.SHANGHAI).date() + timedelta(days=1),
                       str(uuid.uuid4()), '延后5分钟')
        await session.commit()

    token, _ = await create_token('briefing-test')
    principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer ' + token}) as client:
            at = day - timedelta(days=30)
            body = {'kind': 'daily', 'report_date': str(at), 'request_id': str(uuid.uuid4()), 'regenerate': True}
            responses = await asyncio.gather(*(client.post('/api/briefings/reports/generate', json=body) for _ in range(2)))
            assert all(item.status_code == 200 for item in responses), [item.text for item in responses]
            assert responses[0].json()['data'] == responses[1].json()['data']
            daily_id = responses[0].json()['data']['id']
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            periods = list(await session.scalars(select(Report).where(Report.report_date == at, Report.kind != 'daily')))
            assert {item.kind for item in periods} == set(schedule.PERIOD_KINDS)
            assert len(periods) == 3 and all(item.request_id and not item.regenerate for item in periods)
            settings = await config.settings_row(session, lock=True)
            await reports.prepare_due(session, settings, now)
            actions = await candidates(session, settings.config)
            assert all(('report', item.id, 'historical_backfill') in actions for item in periods)
            daily = await session.get(Report, daily_id)
            assert not await reports.prepare_daily(session, daily, now + timedelta(hours=2))
            assert daily.state == 'waiting_backfill'
            daily.next_check_at = now  # 上面的未来时钟仅验证依赖等待，不延后后续当前时钟验收。
            await session.commit()
        # 空窗口完整走到成功；不改读原文、不发送真实或合成模型请求。
        with patch.object(model, 'complete', side_effect=AssertionError('空窗口不应调用模型')) as forbidden:
            for item in periods:
                await reports.process_report(item.id, revision)
            async with SessionLocal() as session:
                settings = await config.settings_row(session, lock=True)
                await reports.prepare_due(session, settings, now + timedelta(seconds=16))
                daily = await session.get(Report, daily_id)
                assert {item['report_id'] for item in daily.input['dependencies']} == {item.id for item in periods}
                assert daily.input['analysis_ids'] == []
                await session.commit()
            await reports.process_report(daily_id, revision)
            forbidden.assert_not_called()
        async with SessionLocal() as session:
            assert (await session.get(Report, daily_id)).state == 'done'
            # 复用成功版本和在途请求；给自动依赖固定手动准入，收窄日期后仍可执行。
            at = day - timedelta(days=40)
            done = await add_report(session, 'morning', at, 'done')
            done.output = {'highlight_analysis_ids': []}
            pending = await add_report(session, 'evening', at)
            processing = await add_report(session, 'intraday', at, 'processing')
            processing.control_revision, processing.started_at = revision, now
            frozen, first_check = dict(pending.input), pending.first_check_at
            daily = await reports.request_report(session, 'daily', at, str(uuid.uuid4()), True)
            assert done.request_id is None
            assert pending.state == 'pending' and pending.input == frozen and pending.first_check_at == first_check
            assert pending.request_id and processing.request_id
            assert processing.state == 'processing' and processing.control_revision == revision and processing.started_at == now
            assert await session.scalar(select(func.count()).select_from(Report).where(Report.report_date == at)) == 4
            settings = await config.settings_row(session, lock=True)
            today = str(datetime.now(schedule.SHANGHAI).date())
            settings.config = {**settings.config, 'backfill_start': today}
            scoped = set(await session.scalars(select(Report.id).where(service.report_scope(settings.config))))
            assert {daily.id, pending.id, processing.id} <= scoped
            assert ('report', pending.id, 'historical_backfill') in await candidates(session, settings.config)
            # 另一个手动日报不会覆写已有手动分时报的UUID。
            at = day - timedelta(days=41)
            identifier = str(uuid.uuid4())
            existing = await add_report(session, 'morning', at, request_id=identifier)
            await reports.request_report(session, 'daily', at, str(uuid.uuid4()))
            assert existing.request_id == identifier and existing.state == 'pending'
            assert await session.scalar(select(func.count()).select_from(Report).where(Report.report_date == at)) == 4
            await session.commit()
    finally:
        await revoke_session(principal)


async def main():
    prefix = 'briefing-test-' + uuid.uuid4().hex[:12]
    day = datetime.now(schedule.SHANGHAI).date() - timedelta(days=3)
    now = datetime.now(UTC)
    if '--migrate' in sys.argv:
        # 首先在旧 head 放入一条历史记录，再运行新增迁移，证明不会重写旧审计。
        async with engine.begin() as connection:
            await connection.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'f07890123456'], check=True)
        async with SessionLocal() as session:
            await session.execute(text("INSERT INTO sources (code,name,list_url,enabled,interval_seconds,backfill_hours,consecutive_failures) VALUES (:code,'合成来源','https://example.invalid',false,300,72,0)"), {'code': prefix})
            old = NewsItem(source_code=prefix, external_id='legacy', url='https://example.invalid/legacy', title='旧新闻',
                summary='', content_text='旧正文', content_html='<p>旧正文</p>', content_hash='a' * 64,
                published_at=now, first_seen_at=now)
            session.add(old)
            await session.flush()
            await session.execute(text("INSERT INTO news_research_packages (news_id,content_hash,state,keywords) VALUES (:id,:hash,'done','[]')"), {'id': old.id, 'hash': old.content_hash})
            await session.commit()
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], check=True)
    async with SessionLocal() as session:
        assert await session.scalar(text('SELECT version_num FROM alembic_version')) == 'f63456789012'
        settings = await config.settings_row(session, lock=True)
        assert not settings.enabled
        old_count = await session.scalar(text('SELECT count(*) FROM news_research_packages'))
        if '--migrate' not in sys.argv:
            session.add(Source(code=prefix, name='合成来源', list_url='https://example.invalid', enabled=False, interval_seconds=300))
            await session.flush()
        provider = AiProvider(name=prefix, protocol='openai', base_url='http://127.0.0.1:1',
            api_key_encrypted=encrypt('fake-model-key'), api_key_hint='fake', model='synthetic', context_window=250000, max_tokens=8192)
        session.add(provider)
        await session.flush()
        configuration = {}
        for stage in ('analysis', 'period', 'daily'):
            skill = SkillNode(name=prefix + stage, kind='skill', scope='news', enabled=True)
            session.add(skill)
            await session.flush()
            version = SkillVersion(skill_id=skill.id, version_no=1, source='user',
                content=contracts.SKILL_TEMPLATES[stage][1] + '\n合成自定义规则：初始化不得覆盖本文。')
            session.add(version)
            await session.flush()
            skill.published_version_id = version.id
            configuration[stage] = {'provider_id': provider.id, 'model': 'synthetic', 'reasoning_effort': 'low',
                'skill_id': skill.id, 'max_tokens': 4096 if stage == 'analysis' else 8192}
        configuration['concurrency'] = 2
        settings.config = configuration
        settings.enabled, settings.control_revision, settings.enabled_at = True, 1, now
        ids = []
        for index, hour in enumerate((1, 12, 21)):
            at = datetime.combine(day, datetime.min.time(), schedule.SHANGHAI) + timedelta(hours=hour)
            item = NewsItem(source_code=prefix, external_id=str(index), url=f'https://example.invalid/{index}',
                title='ETH 上涨5%', summary='合成来源', content_text='ETH 上涨5%，尚待核实。',
                content_html='<p>ETH 上涨5%，尚待核实。</p>', content_hash=str(index + 1) * 64,
                revision_no=1, published_at=at, first_seen_at=at + timedelta(seconds=1))
            session.add(item)
            await session.flush()
            ids.append(item.id)
        await session.commit()
        await config.validate_config(session, configuration)
        detached, _, _ = await config.model_config(session, configuration, 'analysis')
        assert detached.max_tokens == 4096 and provider.max_tokens == 8192
        await service.enroll(session, settings, now)
        await session.commit()
        initial = list(await session.scalars(select(Analysis).where(Analysis.news_id.in_(ids)).order_by(Analysis.news_id)))
        assert len(initial) == 3
        await service.enroll(session, settings, now)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Analysis).where(Analysis.news_id.in_(ids))) == 3

    calls = []
    async def complete(provider, system, payload, schema, *, allow_repair=True):
        calls.append((schema.__name__, payload))
        if schema in (contracts.NewsAnalysisResult, contracts.NewsAnalysisDraft):
            result = schema(keywords=[{'canonical': '以太坊', 'aliases': ['ETH'], 'kind': 'asset', 'quote': 'ETH'}],
                summary='ETH 报道摘要', facts=[{'text': '报道上涨5%', 'quote': '上涨5%'}], impact='待持续观察',
                importance_score=75, importance_reason='影响市场', primary_region={'code': 'CRYPTO', 'name': '加密货币'},
                related_regions=[], assets=[], uncertainties=['数据为测试'], scenarios=[])
        else:
            allowed = ([card['analysis_id'] for card in payload['cards']] if 'cards' in payload
                       else payload.get('analysis_ids', []))
            result = schema(title='以太坊合成报告', summary='合成汇总', sections=[], highlight_analysis_ids=allowed[:3])
        return result, {'input_tokens': 100, 'output_tokens': 20}
    original_complete = model.complete
    model.complete = complete
    for row in initial:
        await progress.execute(('analysis', row.id), service.process_analysis(row.id, 1))
    assert len(calls) == 3
    async with SessionLocal() as session:
        assert all(row.state == 'done' and row.importance_stars == 4 for row in
                   await session.scalars(select(Analysis).where(Analysis.news_id.in_(ids))))
        news = await session.get(NewsItem, ids[0])
        session.add(Source(code=prefix + '-v2', name='修订后的合成来源', list_url='https://example.invalid/new',
                           enabled=False, interval_seconds=300))
        await session.flush()
        news.title, news.content_text, news.content_hash, news.revision_no = '原文更新', '新正文', 'f' * 64, 2
        news.source_code, news.url = prefix + '-v2', 'https://example.invalid/changed'
        news.first_seen_at += timedelta(minutes=1)
        await service.enroll(session, await config.settings_row(session, lock=True), now)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Analysis).where(Analysis.news_id == ids[0])) == 1
        row = await session.get(Analysis, initial[0].id)
        assert row.input['title'] == 'ETH 上涨5%' and row.source_revision == 1
        async def search_analyses(query):
            return (await api.analyses(session, page=1, limit=100, news_id=None, state=None,
                                      q=query, stars=None, region=None)).data['items']
        assert set(ids) <= {item['news_id'] for item in await search_analyses('ETH 上涨5%')}
        assert not await search_analyses('5_')  # LIKE 通配符必须按字面匹配。
        assert ids[0] not in {item['news_id'] for item in await search_analyses('原文更新')}
        # 发布时间双向跨越 09:00，既有分析仍按自己的冻结时间归窗。
        other = await session.get(NewsItem, ids[1])
        news.published_at, other.published_at = other.published_at, news.published_at
        await session.commit()
        # 新闻更新只显示版本变化，不触发再分析；新闻查询仍能命中关键词和别名。
        from alphapulse.api.news import news_filters
        for query in ('以太坊', 'ETH'):
            assert ids[0] in list(await session.scalars(select(NewsItem.id).where(*news_filters(None, query, None, None))))
        assert ids[0] not in list(await session.scalars(select(NewsItem.id).where(*news_filters(None, 'DETH', None, None))))
        from alphapulse.api.news import list_news
        filtered = (await list_news(session, N(username='briefing-test'), processing_state='done')).data.items
        assert set(ids) <= {row.id for row in filtered}
        selected_columns = []
        def record_analysis_columns(conn, cursor, statement, parameters, context, executemany):
            if 'briefing_analyses' in statement and cursor.description:
                selected_columns.extend(column[0] for column in cursor.description)
        event.listen(engine.sync_engine, 'after_cursor_execute', record_analysis_columns)
        try:
            await reports.prepare_due(session, await config.settings_row(session, lock=True), now)
        finally:
            event.remove(engine.sync_engine, 'after_cursor_execute', record_analysis_columns)
        assert selected_columns and 'input' not in selected_columns
        await session.commit()
        periods = list(await session.scalars(select(Report).where(Report.report_date == day, Report.kind != 'daily')))
        assert len(periods) == 3 and all(row.input is not None for row in periods)
        source_fields = ('source_code', 'url', 'title', 'published_at', 'first_seen_at')
        for period in periods:
            index = ('morning', 'evening', 'intraday').index(period.kind)
            expected, frozen = initial[index], calls[index][1]
            assert period.input['analysis_ids'] == [expected.id]
            assert period.input['cards'][0]['source'] == {key: frozen[key] for key in source_fields}
            assert period.input['cards'][0]['source_revision'] == frozen['revision_no']
            assert all(period.window_start <= datetime.fromisoformat(card['effective_at']) < period.window_end
                       for card in period.input['cards'])
        assert all('content_text' not in json.dumps(row.input['cards'], ensure_ascii=False) for row in periods)
    for row in periods:
        await progress.execute(('report', row.id), reports.process_report(row.id, 1))
    assert len(calls) == 6
    async with SessionLocal() as session:
        for identifier in [row.id for row in periods]:
            finished = await session.get(Report, identifier)
            assert finished.state == 'done', (finished.kind, finished.state, finished.error)
        await reports.prepare_due(session, await config.settings_row(session, lock=True), now + timedelta(seconds=16))
        await session.commit()
        daily = await session.scalar(select(Report).where(Report.report_date == day, Report.kind == 'daily'))
        assert len(daily.input['dependencies']) == 3 and len(daily.input['highlights']) == 3
        assert daily.input['highlights'][0]['article']['title'] == 'ETH 上涨5%'
        assert 'cards' not in daily.input
        daily_id = daily.id
    await progress.execute(('report', daily_id), reports.process_report(daily_id, 1))
    assert len(calls) == 7
    async with SessionLocal() as session:
        daily = await session.get(Report, daily_id)
        assert daily.state == 'done' and daily.output['title'] == '以太坊合成报告'
        # 成功版本升级须同时切换引用与归窗；未分析新闻仍进入覆盖检查。
        async with session.begin_nested() as transaction:
            previous = await session.get(Analysis, initial[1].id)
            changed_at = datetime.combine(day, datetime.min.time(), schedule.SHANGHAI) + timedelta(hours=10)
            frozen = {**calls[1][1], 'title': '新版冻结标题', 'url': 'https://example.invalid/frozen-v2',
                      'published_at': changed_at.isoformat(), 'revision_no': 2}
            updated = Analysis(news_id=ids[1], version_no=2, state='done', effective_at=changed_at,
                time_basis='published_at', input=frozen, source_revision=2, output=previous.output, importance_stars=4)
            session.add(updated)
            await session.flush()
            left, right, _ = schedule.window('evening', day)
            probe = Report(kind='evening', report_date=day, window_start=left, window_end=right,
                           due_at=now, first_check_at=now, lane='live')
            assert await service.prepare_period(session, probe, now)
            assert probe.input['analysis_ids'] == [updated.id]
            assert probe.input['cards'][0]['effective_at'] == updated.effective_at.isoformat()
            assert probe.input['cards'][0]['source_revision'] == 2
            assert probe.input['cards'][0]['source'] == {key: frozen[key] for key in source_fields}
            # 缺少冻结元数据的旧版本保持未知，不改读当前新闻补齐来源。
            unknown = Analysis(news_id=ids[1], version_no=3, state='done', effective_at=changed_at,
                time_basis='published_at', output=previous.output, importance_stars=4)
            session.add(unknown)
            await session.flush()
            assert await service.prepare_period(session, probe, now)
            assert probe.input['analysis_ids'] == [unknown.id]
            assert probe.input['cards'][0]['source_revision'] is None
            assert probe.input['cards'][0]['source'] == dict.fromkeys(source_fields)
            pending_news = NewsItem(source_code=prefix, external_id='window-pending', title='待分析新闻',
                url='https://example.invalid/pending', summary='', content_text='待分析', content_hash='d' * 64,
                published_at=left, first_seen_at=left)
            session.add(pending_news)
            await session.flush()
            probe = Report(kind='evening', report_date=day, window_start=left, window_end=right,
                           due_at=now, first_check_at=now, lane='live')
            assert not await service.prepare_period(session, probe, now)
            assert probe.coverage['missing_news_ids'] == [pending_news.id]
            await transaction.rollback()
        result = await api.report(daily_id, session)
        assert result.data['usage']['input_tokens'] == 100
        assert result.data['usage']['output_tokens'] == 20
        by_title = await api.list_reports(session, page=1, limit=100, q='以太坊', kind=None, start=None, end=None, region=None, stars=None)
        assert daily_id in [row['id'] for row in by_title.data['items']]
        req = str(uuid.uuid4())
        newer = await reports.request_report(session, 'morning', day, req, False)
        new_id = newer.id
        assert (await reports.request_report(session, 'morning', day, req, False)).id == new_id
        assert newer.version_no == 2
        assert await service.prepare_period(session, newer, now)
        newer.market = next(row.market for row in periods if row.kind == 'morning')
        await session.commit()
    # 先前已成功、输入/契约完全相同的步骤不产生新模型账单。
    before = len(calls)
    await progress.execute(('report', new_id), reports.process_report(new_id, 1))
    assert len(calls) == before
    async with SessionLocal() as session:
        part = await session.scalar(select(ReportPart).where(ReportPart.report_id == new_id))
        assert part.state == 'done' and part.reused_from and part.usage is None
        assert (await api.report(new_id, session)).data['usage']['input_tokens'] == 0
        # 手动重分析保留原审计，最新状态检索不混入旧成功版本。
        second_id = await service.retry_analysis(session, initial[0].id)
        assert await service.retry_analysis(session, initial[0].id) == second_id
        await session.commit()
        pending = await api.analyses(session, page=1, limit=100, news_id=None, state='pending',
                                     q='原文更新', stars=None, region=None)
        assert second_id in {item['id'] for item in pending.data['items']}
        done_ids = (await list_news(session, N(username='briefing-test'), processing_state='done')).data.items
        assert ids[0] not in {row.id for row in done_ids}
        assert await session.scalar(text('SELECT count(*) FROM news_research_packages')) == old_count

    # 在真实 DB claim 中发布可观察的精确等待，再从另一个 Redis 客户端写 wake。
    from redis.asyncio import Redis
    original_redis, original_key = progress.redis, progress.SNAPSHOT_KEY
    progress.SNAPSHOT_KEY = prefix + ':snapshot'
    peer = Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
    async def waiting():
        progress.bind_claim('analysis', second_id)
        with progress.retry_wait(6, (now + timedelta(seconds=180)).isoformat()) as wake:
            await progress.sync_once('synthetic-worker')
            snapshot = json.loads(await peer.get(progress.SNAPSHOT_KEY))
            target = snapshot['tasks'][0]
            assert 0 < await peer.ttl(progress.SNAPSHOT_KEY) <= 10
            async with SessionLocal() as session:
                status = await queue.queue_view(session)
                assert status['progress_status'] == 'available'
                item = next(row for row in status['items'] if row['claim']['id'] == second_id)
                body = queue.ResetWaits.model_validate({'targets': [{'task_id': item['task_id'], 'wait_id': item['progress']['wait_id']}]})
                assert len((await queue.reset_waits(session, body))['scheduled']) == 1
                assert len((await queue.reset_waits(session, body))['already_requested']) == 1
            command = progress.WAKE_PREFIX + target['wait_id']
            assert 0 < await peer.ttl(command) <= 90
            await progress.sync_once('synthetic-worker')
            await asyncio.wait_for(wake.wait(), 1)
            await peer.delete(command)
        with progress.retry_wait(7, (now + timedelta(seconds=360)).isoformat()) as next_wake:
            await progress.sync_once('synthetic-worker')
            assert not next_wake.is_set()
    async with SessionLocal() as session:
        row = await session.get(Analysis, second_id)
        row.state, row.control_revision, row.started_at = 'processing', 1, now
        await session.commit()
    # app_env 不参与独立 smoke，实际 sync_once 直接使用一次性 Redis。
    try:
        await progress.execute(('analysis', second_id), waiting())
    finally:
        await peer.delete(progress.SNAPSHOT_KEY)
        progress.SNAPSHOT_KEY, progress.redis = original_key, original_redis
        await peer.aclose()

    # 真实事务所有权：维护只关准入仍允许发布，显式撤销后拒绝旧请求。
    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        settings.enabled, settings.control_revision = False, 2
        row = await session.get(Analysis, second_id)
        assert await service.owns(session, row, 1)
        await service.revoke(session)
        assert not await service.owns(session, row, 1)
        settings.enabled = True
        settings.telegram_enabled, settings.telegram_chat_id = True, '-100123456'
        settings.telegram_token = encrypt('123456789:synthetic-telegram-token-value').decode()
        await notifications.enqueue(session, 'test', prefix + ':ok', '本地测试')
        await notifications.enqueue(session, 'test', prefix + ':ok', '不能重复')
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Notification).where(Notification.dedup_key == prefix + ':ok')) == 1
    outcomes = [(200, {'ok': True}), (429, {'ok': False, 'parameters': {'retry_after': 1}}), (503, {'ok': False})]
    original_client = httpx.AsyncClient
    handlers = set()
    async def fake_tg(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
            assert json.loads(await reader.readexactly(length))['chat_id'] == '-100123456'
            status, body = outcomes.pop(0)
            if status == 0:
                await asyncio.sleep(.2)  # 实际响应超时，结果未知，禁止自动重发。
                return
            payload = json.dumps(body).encode()
            writer.write(f'HTTP/1.1 {status} Test\r\nContent-Length: {len(payload)}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n'.encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())
    server = await asyncio.start_server(fake_tg, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    class LocalTelegramTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.AsyncHTTPTransport()
        async def handle_async_request(self, request):
            assert '/bot123456789:' in request.url.path
            local = httpx.Request(request.method, request.url.copy_with(scheme='http', host='127.0.0.1', port=port),
                                  headers=request.headers, content=request.content, extensions=request.extensions)
            return await self.transport.handle_async_request(local)
        async def aclose(self):
            await self.transport.aclose()
    def client_factory(**kwargs):
        kwargs['timeout'] = .05
        return original_client(transport=LocalTelegramTransport(), **kwargs)
    try:
        assert await notifications.send_one(client_factory=client_factory)
        async with SessionLocal() as session:
            sent = await session.scalar(select(Notification).where(Notification.dedup_key == prefix + ':ok'))
            assert sent.state == 'sent' and sent.attempts == 1
            await notifications.enqueue(session, 'test', prefix + ':429', '本地测试429')
            await session.commit()
        await notifications.send_one(client_factory=client_factory)
        async with SessionLocal() as session:
            retry = await session.scalar(select(Notification).where(Notification.dedup_key == prefix + ':429'))
            assert retry.state == 'pending' and retry.attempts == 1
            retry.next_attempt_at = now - timedelta(seconds=1)
            await session.commit()
        await notifications.send_one(client_factory=client_factory)
        async with SessionLocal() as session:
            unknown = await session.scalar(select(Notification).where(Notification.dedup_key == prefix + ':429'))
            assert unknown.state == 'unknown' and unknown.attempts == 2
            await notifications.recover(session)
            await session.commit()
        # 明确未发出的连接错误只尝试三次；本地服务响应超时只发送一次。
        def connect_failure(request):
            raise httpx.ConnectError('synthetic-before-send', request=request)
        def unavailable(**kwargs):
            return original_client(transport=httpx.MockTransport(connect_failure), **kwargs)
        async with SessionLocal() as session:
            await notifications.enqueue(session, 'test', prefix + ':connect', '本地连接失败')
            await session.commit()
        for attempt in range(1, 4):
            await notifications.send_one(client_factory=unavailable)
            async with SessionLocal() as session:
                failed = await session.scalar(select(Notification).where(Notification.dedup_key == prefix + ':connect'))
                assert failed.attempts == attempt and failed.state == ('pending' if attempt < 3 else 'failed')
                failed.next_attempt_at = now - timedelta(seconds=1)
                await session.commit()
        outcomes.append((0, {}))
        async with SessionLocal() as session:
            await notifications.enqueue(session, 'test', prefix + ':timeout', '本地响应超时')
            await session.commit()
        await notifications.send_one(client_factory=client_factory)
        async with SessionLocal() as session:
            timed_out = await session.scalar(select(Notification).where(Notification.dedup_key == prefix + ':timeout'))
            assert timed_out.state == 'unknown' and timed_out.attempts == 1
        assert not await notifications.send_one(client_factory=client_factory)
        record = logging.LogRecord('httpx', logging.INFO, '', 0, 'POST %s',
            ('https://api.telegram.org/bot123456789:synthetic-telegram-token-value/sendMessage',), None)
        notifications.TelegramURLFilter().filter(record)
        assert 'synthetic-telegram-token-value' not in record.getMessage()
    finally:
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        model.complete = original_complete
    # HTTP 边界：鉴权、严格参数、旧写退役、只读历史和脱敏配置。
    from alphapulse.api.main import app
    from alphapulse.core.auth import create_token, require_session, revoke_session
    from fastapi.security import HTTPAuthorizationCredentials
    token, _ = await create_token('briefing-test')
    principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/briefings/reports')).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + token
        before = len(calls)
        for endpoint in ('settings', 'coverage', 'skill-templates', 'queue', 'reports', 'analyses', 'notifications', 'telegram'):
            response = await client.get('/api/briefings/' + endpoint)
            assert response.status_code == 200, (endpoint, response.text)
            assert 'synthetic-telegram-token-value' not in response.text
        assert len(calls) == before
        assert (await client.get('/api/briefings/reports?kind=invalid')).status_code == 422
        assert (await client.post('/api/briefings/reports/generate', json={'kind': 'daily', 'report_date': str(day), 'request_id': 'invalid'})).status_code == 422
        assert (await client.post('/api/briefings/enable', json={'base_revision': 0})).status_code == 409
        for endpoint in ('packages', 'events', 'settings'):
            assert (await client.get('/api/risk/' + endpoint)).status_code == 404
        assert (await client.post('/api/risk/enable')).status_code == 404
    await revoke_session(principal)

    # 相互独立的事务竞争同一新闻/窗口，只产生一个活跃版本。
    async def competing_report():
        async with SessionLocal() as session:
            try:
                item = await reports.request_report(session, 'morning', day, str(uuid.uuid4()))
                await session.commit()
                return item.id
            except ValueError:
                await session.rollback()
                return None
    identifiers = await asyncio.gather(competing_report(), competing_report())
    assert len([identifier for identifier in identifiers if identifier]) == 1
    async def competing_analysis():
        async with SessionLocal() as session:
            identifier = await service.retry_analysis(session, initial[0].id)
            await session.commit()
            return identifier
    assert len(set(await asyncio.gather(competing_analysis(), competing_analysis()))) == 1

    from unittest.mock import patch
    # 所有别名均无原文依据时仍在持久化前失败，并保留实际调用用量。
    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        settings.enabled = True
        alias_revision = settings.control_revision
        invalid_alias = await service.retry_analysis(session, initial[2].id)
        await session.commit()
    async def fabricated_alias(provider, system, payload, schema, *, allow_repair=True):
        output, usage = await complete(provider, system, payload, schema)
        output.keywords[0].aliases = ['以太坊']
        return output, usage
    with patch.object(model, 'complete', side_effect=fabricated_alias):
        await service.process_analysis(invalid_alias, alias_revision)
    async with SessionLocal() as session:
        failed = await session.get(Analysis, invalid_alias)
        assert failed.state == 'failed' and '关键词无可保留的原文别名' in failed.error and failed.output is None
        assert failed.usage == {'rounds': [{'input_tokens': 100, 'output_tokens': 20}] * 2}
        assert failed.input['content_text'] == 'ETH 上涨5%，尚待核实。'
        assert (await session.get(Analysis, initial[2].id)).state == 'done'

    # 完整输入超预算在网络前失败；被撤销的旧请求即使完成也不能发布。
    async with SessionLocal() as session:
        settings = await config.settings_row(session, lock=True)
        settings.enabled = True
        revision = settings.control_revision
        oversized = await service.retry_analysis(session, initial[0].id)
        await session.commit()
    with patch.object(model, 'fits_context', return_value=False), patch.object(model, 'complete') as forbidden:
        await service.process_analysis(oversized, revision)
        forbidden.assert_not_called()
    async with SessionLocal() as session:
        failed = await session.get(Analysis, oversized)
        assert failed.state == 'failed' and '未发送模型请求' in failed.error
        assert failed.input['content_text'] and failed.output is None
        revoked = await service.retry_analysis(session, oversized)
        await session.commit()
    async def late_complete(provider, system, payload, schema, *, allow_repair=True):
        async with SessionLocal() as other:
            await service.revoke(other, '合成并发撤销')
            await other.commit()
        return await complete(provider, system, payload, schema)
    with patch.object(model, 'complete', side_effect=late_complete):
        await service.process_analysis(revoked, revision)
    async with SessionLocal() as session:
        interrupted = await session.get(Analysis, revoked)
        assert interrupted.state == 'interrupted' and interrupted.output is None
        assert (await session.get(Analysis, initial[0].id)).state == 'done'

    # 原生维护在真实事务中同时处理两套开关，旧构建回滚只恢复旧开关。
    import tempfile
    from alembic.script import ScriptDirectory
    maintenance_path = Path(__file__).resolve().parent / 'maintenance.py' if '__file__' in globals() else Path('/tmp/maintenance.py')
    if not maintenance_path.exists():
        raise RuntimeError('隔离维护验收缺少 maintenance.py，请将其放在本脚本同目录')
    if maintenance_path.exists():
        maintenance = runpy.run_path(str(maintenance_path))
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / 'journal.json'
            journal.write_text('{}')
            async with SessionLocal() as session:
                await session.execute(text("INSERT INTO news_research_settings (id,config,enabled,start_at) VALUES (1,'{}',true,now()) ON CONFLICT(id) DO UPDATE SET enabled=true"))
                settings = await config.settings_row(session, lock=True)
                settings.enabled = True
                await session.commit()
            await maintenance['execute']('pause', journal, '')
            await maintenance['execute']('assert-paused', journal, '')
            await maintenance['execute']('restore', journal, '')
            async with SessionLocal() as session:
                assert not await session.scalar(text('SELECT enabled FROM news_research_settings WHERE id=1'))
                assert (await config.settings_row(session)).enabled
                await session.execute(text('UPDATE news_research_settings SET enabled=true WHERE id=1'))
                await session.commit()
            await maintenance['execute']('pause', journal, '')
            with patch.object(ScriptDirectory, 'from_config', return_value=N(get_heads=lambda: ['f07890123456'])):
                await maintenance['execute']('restore-rollback', journal, '')
            async with SessionLocal() as session:
                assert await session.scalar(text('SELECT enabled FROM news_research_settings WHERE id=1'))
                assert not (await config.settings_row(session)).enabled
    await configuration_acceptance(day, configuration, now)
    await global_backfill_acceptance(day, now)
    await manual_backfill_acceptance(day, now)
    await legacy_cursor_acceptance(day, now)
    await manual_analysis_acceptance(day, now, complete)
    # 多服务和爬虫子进程不能各自常驻五个空闲连接；并发请求结束后只保留一个。
    ready = asyncio.Event()
    active = []
    async def connection_burst():
        async with engine.connect() as connection:
            active.append(await connection.scalar(text('SELECT pg_backend_pid()')))
            if len(active) == 5:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)
    await asyncio.gather(*(connection_burst() for _ in range(5)))
    assert len(set(active)) == 5
    assert engine.pool.checkedin() == 1 and engine.pool.checkedout() == 0
    await redis.aclose()
    await engine.dispose()
    print('Briefing acceptance passed', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
