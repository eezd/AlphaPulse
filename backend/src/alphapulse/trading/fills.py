"""同步币安成交记录：历史仓位由它还原（见 positions.py），包括程序外的手动成交"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.models import TradingFill
from alphapulse.trading.client import DemoClient

PAGE = 1000
# 币安只保留近 3 个月的成交和资金流水
HISTORY_DAYS = 90
# 流水时间只精确到秒：往前多取一段，已有的成交按 trade id 去重
OVERLAP = timedelta(minutes=1)


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def missing_starts(session: AsyncSession, client: DemoClient) -> dict[str, int]:
    """用手续费流水（每笔成交一条，不限币种）发现新成交：返回每个币库里还没有的最小 trade id"""
    latest = await session.scalar(select(func.max(TradingFill.time)).where(TradingFill.exchange == client.exchange))
    start = to_ms(latest - OVERLAP if latest else datetime.now(UTC) - timedelta(days=HISTORY_DAYS))
    found: dict[str, set[int]] = {}
    while True:
        rows = await client.request("GET", "/fapi/v1/income", {"incomeType": "COMMISSION", "startTime": start, "limit": PAGE})
        for row in rows:
            found.setdefault(row["symbol"], set()).add(int(row["tradeId"]))
        if len(rows) < PAGE:
            break
        if rows[-1]["time"] == start:
            raise RuntimeError("同一秒内的手续费流水超过一页，无法按时间翻页")
        start = rows[-1]["time"]
    starts = {}
    for symbol, ids in found.items():
        known = {int(v) for v in await session.scalars(select(TradingFill.trade_id).where(TradingFill.exchange == client.exchange, TradingFill.symbol == symbol, TradingFill.trade_id.in_([str(i) for i in ids])))}
        if ids - known:
            starts[symbol] = min(ids - known)
    return starts


async def sync_fills(session: AsyncSession, client: DemoClient, symbols: set[str]) -> int:
    """把新成交写入 trading_fills，返回拉到的成交数。symbols 为交易列表和当前持仓的币：
    每轮按库里最大 trade id 往后查一次，兜底手续费为 0、没有流水的成交"""
    if client.exchange == "bybit":
        latest = await session.scalar(select(func.max(TradingFill.time)).where(TradingFill.exchange == client.exchange))
        start = latest - timedelta(minutes=10) if latest else datetime.now(UTC) - timedelta(days=7)
        trades = await client.trades(None, to_ms(start))
        await save_fills(session, client.exchange, trades)
        await session.commit()
        return len(trades)
    starts = await missing_starts(session, client)
    for symbol in symbols:
        last = await session.scalar(select(func.max(cast(TradingFill.trade_id, BigInteger))).where(TradingFill.exchange == client.exchange, TradingFill.symbol == symbol))
        if last is not None:
            starts[symbol] = min(starts.get(symbol, last + 1), last + 1)
    fetched = 0
    for symbol, from_id in starts.items():
        while True:
            trades = await client.request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "fromId": from_id, "limit": PAGE})
            if trades:
                await save_fills(session, client.exchange, trades)
                fetched += len(trades)
            if len(trades) < PAGE:
                break
            from_id = trades[-1]["id"] + 1
    await session.commit()
    return fetched


async def save_fills(session: AsyncSession, exchange: str, trades: list[dict]) -> None:
    if not trades:
        return
    await session.execute(insert(TradingFill).values([{
        "exchange": exchange, "symbol": t["symbol"], "trade_id": str(t["id"]), "order_id": str(t["orderId"]), "side": t["side"],
        "price": Decimal(t["price"]), "qty": Decimal(t["qty"]), "realized_pnl": Decimal(t["realizedPnl"]),
        "commission": Decimal(t["commission"]), "commission_asset": t["commissionAsset"],
        "time": datetime.fromtimestamp(t["time"] / 1000, UTC),
    } for t in trades]).on_conflict_do_nothing())
