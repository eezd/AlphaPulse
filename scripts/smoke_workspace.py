"""Browser acceptance for the Stitch workspace in the disposable CI network."""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from sqlalchemy import select
from websockets.asyncio.client import connect

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import redis
from alphapulse.models import Kline, NewsBookmark, NewsItem, SkillNode, SkillVersion
from alphapulse.market.worker import handle_message
from alphapulse.news_research.models import Event, Membership, Package, Version


async def main():
    assert os.environ['DATABASE_URL'].startswith('postgresql+asyncpg://ci:ci-only@ap-ci-')
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        news = NewsItem(source_code='wscn_news', external_id='ci-workspace', url='https://example.invalid/workspace',
                        title='美联储公布利率决议，市场关注流动性变化', summary='隔离环境中的界面验收样本。',
                        content_text='The Federal Reserve announced its rate decision. 市场关注后续政策路径。',
                        content_html='<p>The Federal Reserve announced its rate decision.</p><p>市场关注后续政策路径。</p>',
                        published_at=now, content_hash='f' * 64)
        session.add(news)
        await session.flush()
        package = Package(news_id=news.id, content_hash=news.content_hash, state='done',
                          keywords=[{'canonical': '美联储', 'aliases': ['Federal Reserve'], 'quote': 'Federal Reserve'}])
        result = {'title': news.title, 'summary': '美联储公布最新利率决定；当前样本用于验证事件、证据与时间线的展示。',
                  'facts': '已收录公开声明。', 'reasoning': '依据已采集新闻原文。', 'verification': '本地证据核对。',
                  'uncertainty': '未来政策路径仍存在不确定性。', 'opinions': '关注后续声明。',
                  'counter_evidence': '尚无其他已采集反证。', 'watchpoints': '下一次会议。',
                  'asset_impacts': '利率和美元资产。', 'importance_stars': 4, 'risk_stars': 3,
                  'confidence': '中等', 'timeline_points': []}
        event = Event(title=news.title, state='done', tags=['美联储'], result=result)
        session.add_all([package, event])
        await session.flush()
        session.add(Membership(event_id=event.id, package_id=package.id, evidence='Federal Reserve',
                               reason='同一利率决议', change_kind='new_fact'))
        session.add(Version(event_id=event.id, revision=event.revision, state='done', output=result, rules={},
                            input={'packages': [{'id': package.id, 'news_id': news.id}],
                                   'articles': [{'id': news.id, 'title': news.title, 'summary': news.summary,
                                                 'content': news.content_text, 'url': news.url}]}))
        session.add(NewsBookmark(username='another-user', news_id=news.id))
        skill = SkillNode(kind='skill', scope='shared', name='CI 研究规则')
        session.add(skill)
        await session.flush()
        session.add(SkillVersion(skill_id=skill.id, version_no=1, source='user',
                                 content='# 研究规则\n\n- 使用中文输出\n- 基于原文证据核验，不调用外部搜索。'))
        start = int(now.timestamp() * 1000) // 900000 * 900000 - 100 * 900000
        for i in range(100):
            price = 90 + i / 10
            session.add(Kline(market='um', symbol='BTCUSDT', interval='15m', open_time=start + i * 900000,
                              open=price, close=price + .2, high=price + .5, low=price - .3, volume=100 + i,
                              close_time=start + (i + 1) * 900000 - 1, quote_volume=10000,
                              trades=10, taker_buy_base=50, taker_buy_quote=5000))
        await session.commit()
        news_id, event_id = news.id, event.id
    origin = 'http://' + os.environ['SMOKE_WEB']
    output = Path('/tmp/alphapulse-ui')
    output.mkdir(exist_ok=True)
    async with httpx.AsyncClient(base_url=origin, timeout=15, trust_env=False) as client:
        response = await client.post('/api/auth/login', json={'username': 'ci', 'password': 'ci-only'})
        response.raise_for_status()
        token = response.json()['data']['token']
        client.headers['Authorization'] = 'Bearer ' + token
        try:
            # Test private, idempotent bookmarks and current-content research filters against PostgreSQL.
            assert not (await client.get('/api/news', params={'ids': news_id})).json()['data']['items'][0]['bookmarked']
            for _ in range(2):
                assert (await client.put(f'/api/news/{news_id}/bookmark')).status_code == 200
            saved = (await client.get('/api/news', params={'ids': news_id, 'bookmarked': True})).json()['data']['items']
            assert len(saved) == 1 and saved[0]['keywords'][0]['canonical'] == '美联储'
            assert saved[0]['events'][0]['id'] == event_id
            assert (await client.put('/api/news/9223372036854775807/bookmark')).status_code == 404
            assert (await client.get('/api/news', params={'sort': 'invalid'})).status_code == 422
            assert (await client.get('/api/news', params={'processing_state': 'invalid'})).status_code == 422
            assert (await client.get('/api/news', params={'ids': news_id, 'processing_state': 'done'})).json()['data']['total'] == 1
            async with SessionLocal() as session:
                original = await session.get(NewsItem, news_id)
                original.content_hash = 'e' * 64
                await session.commit()
            assert (await client.get('/api/news', params={'ids': news_id, 'processing_state': 'done'})).json()['data']['total'] == 0
            assert (await client.get('/api/news', params={'ids': news_id, 'processing_state': 'unprocessed'})).json()['data']['total'] == 1
            async with SessionLocal() as session:
                original = await session.get(NewsItem, news_id)
                original.content_hash = 'f' * 64
                await session.commit()
            for _ in range(2):
                assert (await client.delete(f'/api/news/{news_id}/bookmark')).status_code == 200
            async with SessionLocal() as session:
                assert await session.scalar(select(NewsBookmark).where(NewsBookmark.username == 'another-user', NewsBookmark.news_id == news_id))
            filtered = (await client.get('/api/risk/events', params={'q': '美联储', 'state': 'done', 'importance_min': 4})).json()['data']['items']
            assert filtered and filtered[0]['package_count'] == 1 and filtered[0]['source_count'] == 1
            # All upstream feeds stay synthetic; real model/exchange calls are never enabled.
            await redis.set('ap:market:symbols:spot', json.dumps([
                {'symbol': 'BTCUSDT', 'base': 'BTC', 'quote': 'USDT', 'price_precision': 2, 'volume_precision': 3}
            ]), ex=300)
            async def quotes():
                trade_id = 0
                while True:
                    at = int(datetime.now(UTC).timestamp() * 1000)
                    trade_id += 1
                    for stream, payload in [
                        ('depth20@500ms', {'s': 'BTCUSDT', 'E': at, 'b': [['100', '2']], 'a': [['101', '3']]}),
                        ('markPrice@1s', {'s': 'BTCUSDT', 'p': '100', 'i': '99.9', 'r': '.0001', 'T': at + 600000, 'E': at}),
                        ('aggTrade', {'s': 'BTCUSDT', 'a': trade_id, 'p': '100', 'q': '.1', 'T': at, 'm': False}),
                        ('miniTicker', {'e': '24hrMiniTicker', 's': 'BTCUSDT', 'c': '100', 'o': '98',
                                        'h': '101', 'l': '97', 'v': '100', 'q': '10000', 'E': at}),
                    ]:
                        await handle_message('um', json.dumps({'stream': 'btcusdt@' + stream, 'data': payload}))
                    await asyncio.sleep(.3)
            publisher = asyncio.create_task(quotes())
            try:
                ticket = (await client.post('/api/auth/ws-ticket')).json()['data']['ticket']
                async with connect('ws://' + os.environ['SMOKE_WEB'] + '/api/market/ws',
                                   subprotocols=['alphapulse.market.v1', 'ticket.' + ticket], proxy=None) as socket:
                    await socket.send(json.dumps({'market': 'um', 'symbol': 'BTCUSDT', 'interval': '1m', 'quotes': True}))
                    received = set()
                    async with asyncio.timeout(8):
                        while not {'depth', 'trade', 'mark_price'} <= received:
                            received.add(json.loads(await socket.recv())['type'])
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch()
                    context = await browser.new_context(viewport={'width': 1600, 'height': 1100}, device_scale_factor=1)
                    await context.add_init_script('if (window === window.top) localStorage.setItem("alphapulse.auth.token", ' + json.dumps(token) + ')')
                    page = await context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    for route in ('home', 'news', 'calendar', 'crawler', 'market', 'trading', 'chat', 'skills', 'settings'):
                        await page.goto(origin + '/' + route)
                        await page.locator('.terminal-shell').wait_for()
                        await page.wait_for_timeout(600)
                        await page.screenshot(path=str(output / (route + '.png')), full_page=True)
                        assert await page.locator('.terminal-header .active').count() == 1, route
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), route
                    await page.goto(origin + '/market')
                    await page.get_by_role('button', name='全屏', exact=True).click()
                    await page.wait_for_function('document.fullscreenElement === document.documentElement')
                    await page.locator('.symbol-select').click()
                    await page.locator('.el-select-dropdown__item.is-selected').click()
                    await page.get_by_text('指标 ▾', exact=True).click()
                    await page.locator('label.el-checkbox-button').filter(has=page.get_by_role('checkbox', name='MA', exact=True)).click()
                    await page.get_by_text('指标 ▾', exact=True).click()
                    await page.get_by_role('button', name='新增交易对', exact=True).click()
                    await page.get_by_role('dialog', name='新增交易对').get_by_role('button', name='取消', exact=True).click()
                    await page.get_by_role('button', name='全屏', exact=True).click()
                    await page.wait_for_function('document.fullscreenElement === null')
                    await page.goto(origin + '/skills')
                    await page.get_by_text('CI 研究规则', exact=True).click()
                    await page.get_by_placeholder('用 Markdown 写下投资经验、判断流程或输出规范').wait_for()
                    await page.get_by_placeholder('搜索规则名称或路径').fill('研究')
                    await page.get_by_text('搜索期间暂停拖拽排序，清空搜索后恢复。').wait_for()
                    assert await page.locator('.el-tree-node[draggable="true"]').count() == 0
                    await page.get_by_placeholder('搜索规则名称或路径').fill('')
                    assert await page.locator('.el-tree-node[draggable="true"]').count() > 0
                    await page.screenshot(path=str(output / 'skills-editor.png'))
                    await page.goto(origin + '/calendar')
                    await page.locator('.el-table__body .el-table__row').first.click()
                    await page.locator('aside.calendar-detail').get_by_text('事件详情与历史数据').wait_for()
                    await page.screenshot(path=str(output / 'calendar-detail.png'))
                    await page.goto(origin + '/crawler')
                    await page.locator('.source-card').first.click()
                    await page.locator('.crawler-detail').get_by_text('最新入库', exact=False).first.wait_for()
                    await page.screenshot(path=str(output / 'crawler-detail.png'))
                    await page.goto(origin + f'/news?news={news_id}')
                    await page.frame_locator('iframe').get_by_text('The Federal Reserve announced its rate decision.').wait_for()
                    await page.screenshot(path=str(output / 'news-detail.png'), full_page=True)
                    async with page.expect_download() as download:
                        await page.get_by_role('button', name='导出 Markdown', exact=True).click()
                    exported = await download.value
                    assert 'Federal Reserve' in Path(await exported.path()).read_text()
                    await page.get_by_role('button', name='☆ 收藏', exact=True).first.click()
                    await page.get_by_role('button', name='★ 已收藏', exact=True).first.wait_for()
                    await page.reload()
                    await page.get_by_role('button', name='★ 已收藏', exact=True).first.wait_for()
                    await page.goto(origin + f'/home?event={event_id}')
                    await page.get_by_role('tab', name='研究结论', exact=True).wait_for()
                    async with page.expect_download() as download:
                        await page.get_by_role('button', name='导出研究报告', exact=True).click()
                    exported = await download.value
                    report = Path(await exported.path()).read_text()
                    assert '成功研究版本 #' in report and '本版本新闻来源（研究时快照）' in report
                    assert news.url in report
                    for name in ('主体时间线', '证据与新闻包', '版本历史'):
                        await page.get_by_role('tab', name=name, exact=True).click()
                    await page.screenshot(path=str(output / 'event-detail.png'), full_page=True)
                    await page.set_viewport_size({'width': 390, 'height': 844})
                    for route in ('news', 'market', 'skills', 'settings', 'calendar', 'trading', 'home', 'crawler', 'chat'):
                        await page.goto(origin + '/' + route)
                        await page.locator('.terminal-shell').wait_for()
                        await page.wait_for_timeout(300)
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile-' + route
                        await page.screenshot(path=str(output / ('mobile-' + route + '.png')), full_page=True)
                    for width in (1600, 390, 320):
                        await page.set_viewport_size({'width': width, 'height': 844})
                        await page.goto(origin + '/trading')
                        await page.get_by_role('button', name='分析配置', exact=True).click()
                        dialog = page.get_by_role('dialog', name='模拟盘分析配置')
                        await dialog.wait_for()
                        limit = dialog.get_by_role('spinbutton')
                        await limit.click()
                        # 页面不溢出还不足以发现抽屉内部的裁切，直接核对每个控件的边界。
                        assert await dialog.locator('.el-form').evaluate('''form => {
                            const card = form.closest('.el-card').getBoundingClientRect();
                            return [...form.querySelectorAll('.el-select, .el-input-number, button')].every(el => {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.left >= card.left && r.right <= Math.min(card.right, innerWidth);
                            });
                        }'''), f'trading-config-{width}'
                        await dialog.locator('.el-select').first.click()
                        await dialog.locator('.el-select').nth(1).click()
                        await page.get_by_role('option', name='high', exact=True).click()
                        next_limit = int(await limit.input_value()) + 1
                        await limit.fill(str(next_limit))
                        async with page.expect_response('**/api/trading/config') as saving:
                            await dialog.get_by_role('button', name='保存配置', exact=True).click()
                        response = await saving.value
                        assert response.status == 200
                        assert (await response.json())['data']['today']['limit'] == next_limit
                        await dialog.locator('.el-drawer__close-btn').click()
                        await dialog.wait_for(state='hidden')
                    await page.set_viewport_size({'width': 390, 'height': 844})
                    await page.goto(origin + '/market')
                    await page.get_by_role('button', name='盘口／成交').click()
                    await page.get_by_text('买卖各 20 档 · 实时快照').wait_for()
                    await context.close()
                    login = await browser.new_page(viewport={'width': 1600, 'height': 1100})
                    await login.goto(origin + '/login')
                    await login.get_by_placeholder('用户名').wait_for()
                    await login.screenshot(path=str(output / 'login.png'))
                    await browser.close()
                    assert not errors, errors
            finally:
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)
        finally:
            await client.post('/api/auth/logout')
    await redis.aclose()
    print('Workspace bookmarks, filters, event details, 10 desktop pages and 9 mobile pages passed')


asyncio.run(main())
