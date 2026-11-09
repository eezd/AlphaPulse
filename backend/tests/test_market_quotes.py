"""Quote normalization uses Redis only and retains upstream price precision."""

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize('market,stream,data,expected', [
    ('spot', 'btcusdt@depth20@100ms', {'lastUpdateId': 9, 'bids': [['100.001', '2']], 'asks': [['101', '3']]},
     {'bids': [['100.001', '2']], 'asks': [['101', '3']], 'at': 61000}),
    ('um', 'btcusdt@depth20@500ms', {'e': 'depthUpdate', 's': 'BTCUSDT', 'E': 1000, 'b': [['100', '2']], 'a': [['101', '3']]},
     {'bids': [['100', '2']], 'asks': [['101', '3']], 'at': 1000}),
    ('um', 'btcusdt@markPrice@1s', {'s': 'BTCUSDT', 'p': '100.0001', 'i': '99', 'r': '0.00001', 'T': 2000, 'E': 1000},
     {'mark_price': '100.0001', 'index_price': '99', 'funding_rate': '0.00001', 'next_funding_at': 2000, 'at': 1000}),
    ('spot', 'btcusdt@aggTrade', {'s': 'BTCUSDT', 'a': 8, 'p': '100.0001', 'q': '0.00002', 'T': 1000, 'm': True},
     {'id': 8, 'price': '100.0001', 'quantity': '0.00002', 'time': 1000, 'buyer_maker': True}),
])
def test_quote_cache_and_publish(app_env, monkeypatch, market, stream, data, expected):
    from alphapulse.market import worker

    cache = AsyncMock()
    monkeypatch.setattr(worker.time, 'time', lambda: 61)
    monkeypatch.setattr(worker, 'redis', cache)
    persist = AsyncMock()
    monkeypatch.setattr(worker, 'upsert_klines', persist)
    asyncio.run(worker.handle_message(market, json.dumps({'stream': stream, 'data': data})))
    key, value = cache.set.call_args.args
    payload = json.loads(value)
    assert expected.items() <= payload.items()
    assert key.startswith(f'ap:quote:{market}:BTCUSDT:')
    assert json.loads(cache.publish.call_args.args[1]) == payload
    persist.assert_not_called()
    if '@aggTrade' in stream:
        cache.ltrim.assert_awaited_once_with(key + ':recent', 0, 99)
        cache.expire.assert_awaited_once_with(key + ':recent', 300)
        assert cache.set.call_args.kwargs['ex'] == 300
    else:
        assert cache.set.call_args.kwargs['ex'] == 15


@pytest.mark.parametrize('depth_only', [False, True])
def test_futures_depth_uses_public_route_without_requesting_kline_catchup(app_env, monkeypatch, depth_only):
    from alphapulse.market import worker

    urls = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        urls.append(url)
        yield SimpleNamespace(recv=AsyncMock(side_effect=asyncio.CancelledError))

    gate = Mock()
    monkeypatch.setattr(worker, 'catchup_needed', gate)
    monkeypatch.setattr(worker, 'active_symbols', AsyncMock(return_value=['BTCUSDT']))
    monkeypatch.setattr(worker.websockets, 'connect', connect)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.stream_loop('um', depth_only=depth_only))
    if depth_only:
        assert '/public/stream?' in urls[0] and '@depth20@500ms' in urls[0]
        assert '@kline' not in urls[0]
        gate.set.assert_not_called()
    else:
        assert '/market/stream?' in urls[0] and '@aggTrade' in urls[0] and '@markPrice@1s' in urls[0]
        gate.set.assert_called_once()
