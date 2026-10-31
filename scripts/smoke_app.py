"""Exercise the real API in the internal, synthetic CI network only."""

import asyncio
from datetime import UTC, datetime
import json
import os

import httpx
from sqlalchemy import select
from websockets.asyncio.client import connect

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import redis
from alphapulse.models import EconomicRelease, NewsItem


async def main():
    assert os.environ['DATABASE_URL'].startswith('postgresql+asyncpg://ci:ci-only@ap-ci-')
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        if await session.scalar(select(NewsItem.id).where(NewsItem.external_id == 'ci-synthetic')) is None:
            session.add(NewsItem(source_code='wscn_news', external_id='ci-synthetic', url='https://example.invalid/ci',
                                 title='CI synthetic news', summary='Isolated fixture', content_text='CI synthetic news',
                                 content_html='<p>CI synthetic news</p>', published_at=now, content_hash='0' * 64))
            session.add(EconomicRelease(source='bls', external_id='ci-synthetic', family='cpi', title='CI CPI',
                                        period=now.strftime('%Y-%m'), scheduled_at=now, schedule_url='https://example.invalid/ci',
                                        schedule_checked_at=now, next_check_at=now,
                                        values={'cpi_yoy': {'actual': '2.0', 'previous': '2.1'}}))
            await session.commit()
    origin = 'http://' + os.environ['SMOKE_WEB']
    async with httpx.AsyncClient(base_url=origin, timeout=15, trust_env=False) as client:
        response = await client.get('/api/health')
        response.raise_for_status()
        assert response.json()['data']['commit'] == os.environ['APP_COMMIT']
        assert (await client.get('/api/news')).status_code == 401
        response = await client.post('/api/auth/login', json={'username': 'ci', 'password': 'ci-only'})
        response.raise_for_status()
        token = response.json()['data']['token']
        client.headers['Authorization'] = 'Bearer ' + token
        try:
            for path in ['/api/auth/me', '/api/news?q=CI', '/api/crawler/sources', '/api/market/symbols',
                         '/api/skills/tree', '/api/trading/status', '/api/chat/session',
                         '/api/risk/settings', '/api/risk/overview', '/api/risk/summaries',
                         '/api/calendar?start=' + now.strftime('%Y-%m-%d')]:
                response = await client.get(path)
                response.raise_for_status()
                assert response.json()['code'] == 0, path
            assert 'CI synthetic news' in (await client.get('/api/news?q=CI')).text
            for endpoint in ('overview', 'summaries'):
                for hours in (24, 48, 72, 168):
                    response = await client.get('/api/risk/'+endpoint, params={'hours': hours})
                    response.raise_for_status()
                    assert response.json()['code'] == 0
                assert (await client.get('/api/risk/'+endpoint, params={'hours': 25})).status_code == 422
            item = (await client.get('/api/news?q=CI')).json()['data']['items'][0]
            selected = await client.get('/api/news', params=[('ids', item['id']), ('ids', 2147483647)])
            assert selected.status_code == 200 and [n['id'] for n in selected.json()['data']['items']] == [item['id']]
            assert (await client.get('/api/news', params=[('ids', n) for n in range(101)])).status_code == 422
            assert 'CI CPI' in (await client.get('/api/calendar?start=' + now.strftime('%Y-%m-%d'))).text
            for path in ['/', '/home', '/news', '/calendar', '/crawler', '/market', '/trading', '/chat', '/skills', '/settings']:
                response = await client.get(path)
                assert response.status_code == 200 and '<html' in response.text.lower(), path
            assert (await client.get('/version.json')).json()['commit'] == os.environ['APP_COMMIT']
            schema = (await client.get('/api/openapi.json')).json()
            assert 'openapi' in schema and 'code' not in schema
            async with client.stream('GET', '/api/crawler/logs/stream') as stream:
                assert stream.status_code == 200 and 'text/event-stream' in stream.headers['content-type']
                async def publish_log():
                    for _ in range(10):
                        await asyncio.sleep(.15)
                        await redis.publish('ap:pub:logs', json.dumps({'source_code': 'wscn_news', 'message': 'ci-event'}))
                publisher = asyncio.create_task(publish_log())
                try:
                    async with asyncio.timeout(8):
                        async for line in stream.aiter_lines():
                            if line.startswith('data:'):
                                assert 'ci-event' in line
                                break
                finally:
                    publisher.cancel()
                    await asyncio.gather(publisher, return_exceptions=True)
            ticket = (await client.post('/api/auth/ws-ticket')).json()['data']['ticket']
            async with connect('ws://' + os.environ['SMOKE_WEB'] + '/api/market/ws',
                               subprotocols=['alphapulse.market.v1', 'ticket.' + ticket], proxy=None) as socket:
                await socket.send(json.dumps({'market': 'um', 'symbol': 'BTCUSDT', 'interval': '1m'}))
                async def publish_tick():
                    for _ in range(10):
                        await asyncio.sleep(.15)
                        await redis.publish('ap:pub:ticker:um:BTCUSDT', json.dumps({'price': '100'}))
                publisher = asyncio.create_task(publish_tick())
                try:
                    event = json.loads(await asyncio.wait_for(socket.recv(), 8))
                    assert event['type'] == 'ticker' and event['data']['price'] == '100'
                    await client.post('/api/auth/logout')
                    await asyncio.wait_for(socket.wait_closed(), 5)
                    assert socket.close_code == 4401
                finally:
                    publisher.cancel()
                    await asyncio.gather(publisher, return_exceptions=True)
            assert (await client.get('/api/auth/me')).status_code == 401
        finally:
            await client.post('/api/auth/logout')
    await redis.aclose()
    print('Synthetic API, pages, SSE, WS, revocation and persistence passed')


asyncio.run(main())
