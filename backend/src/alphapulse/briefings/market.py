"""只计算本窗口完整的一分钟行情，不以 ticker 或邻近价格补洞。"""
from sqlalchemy import func, select
from alphapulse.models.market import Kline, MarketSymbol


async def snapshot(session, start, end):
    left, right = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    expected = (right - left) // 60000
    result = []
    symbols = await session.scalars(select(MarketSymbol).order_by(MarketSymbol.market, MarketSymbol.symbol))
    for symbol in symbols:
        filters = (Kline.market == symbol.market, Kline.symbol == symbol.symbol, Kline.interval == '1m',
                   Kline.open_time >= left, Kline.open_time < right, Kline.close_time < right)
        count, first_at, last_at = (await session.execute(select(func.count(), func.min(Kline.open_time), func.max(Kline.open_time))
                                                        .where(*filters))).one()
        item = {'market': symbol.market, 'symbol': symbol.symbol, 'source': 'Binance',
                'market_type': '现货' if symbol.market == 'spot' else '永续合约', 'expected_minutes': expected,
                'actual_minutes': count, 'window_start': start.isoformat(), 'window_end': end.isoformat(),
                'first_open_time': first_at, 'last_open_time': last_at, 'status': 'insufficient_data', 'return_pct': None}
        aligned = await session.scalar(select(func.count()).where(*filters, Kline.open_time % 60000 != 0))
        if count == expected and first_at == left and last_at == right - 60000 and aligned == 0:
            first = await session.scalar(select(Kline.open).where(*filters, Kline.open_time == first_at))
            last = await session.scalar(select(Kline.close).where(*filters, Kline.open_time == last_at))
            if first is not None and last is not None and first > 0:
                item.update(status='complete', first_open=str(first), last_close=str(last),
                            return_pct=str((last / first - 1) * 100))
        result.append(item)
    return result
