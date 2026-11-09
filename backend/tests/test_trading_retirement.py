"""Retired endpoints and scoped cleanup must preserve other product data."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/trading/status'), ('PUT', '/api/trading/config'),
    ('POST', '/api/trading/BTCUSDT/start'), ('GET', '/api/settings/exchange'),
    ('PUT', '/api/settings/exchange'), ('DELETE', '/api/settings/exchange'),
    ('POST', '/api/settings/exchange/test'),
])
def test_retired_apis_are_not_registered(app_env, method, path):
    from alphapulse.api.main import app

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.request(method, path)).status_code == 404
    asyncio.run(check())


def test_cleanup_scans_all_pages_and_only_unlinks_trading_keys(app_env):
    from alphapulse.core.remove_trading import cleanup

    async def check():
        pages = [(4, ['ap:trader:status']), (0, []), (0, ['ap:trading:symbols'])]
        for execute in (False, True):
            client = AsyncMock()
            client.scan.side_effect = pages
            client.unlink.return_value = 1
            result = await cleanup(client, execute)
            assert result == {'executed': execute, 'matched': 2, 'removed': 2 if execute else 0}
            assert [c.kwargs for c in client.scan.call_args_list] == [
                {'cursor': 0, 'match': 'ap:trader:*', 'count': 200},
                {'cursor': 4, 'match': 'ap:trader:*', 'count': 200},
                {'cursor': 0, 'match': 'ap:trading:*', 'count': 200},
            ]
            assert [c.args for c in client.unlink.call_args_list] == (
                [('ap:trader:status',), ('ap:trading:symbols',)] if execute else [])
            client.flushdb.assert_not_called()
    asyncio.run(check())
