import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest


def test_forward_endpoint_excludes_labels_at_or_after_entry(trading):
    from alphapulse.api.trading import list_news_signals
    from alphapulse.trading.news_signals import is_forward_sample
    published = datetime(2026, 10, 20, 12, 0, 59, tzinfo=UTC)
    entry = published.replace(second=0) + timedelta(minutes=5)
    assert is_forward_sample(published, entry - timedelta(microseconds=1))
    assert not is_forward_sample(published, entry)
    prices = {symbol: {"entry": "100", "1h": "101", "4h": "102"} for symbol in ("BTCUSDT", "ETHUSDT")}
    rows = [(SimpleNamespace(news_id=i, labeled_at=labeled, category="event", direction="bullish", prices=prices), published, "test", "新闻")
            for i, labeled in enumerate((entry - timedelta(seconds=1), entry, entry + timedelta(hours=5)), 1)]
    class Session:
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: rows)
    result = asyncio.run(list_news_signals(Session())).data
    assert result.labeled == result.filled == 1
    assert [item.news_id for item in result.recent] == [1]
    assert all(s.n == 1 for s in result.stats)


@pytest.mark.parametrize("scenario", ["complete", "missing_close", "partial_entry"])
def test_plan_pnl_includes_early_entry_fee_and_manual_close_only(trading, monkeypatch, scenario):
    from alphapulse.models import TradingFill
    start = datetime(2026, 10, 20, 10, tzinfo=UTC)
    fills = [TradingFill(symbol="BTCUSDT", order_id=order_id, side=side, qty=Decimal(1), price=Decimal(price),
                         realized_pnl=Decimal(pnl), commission=Decimal("0.1"), commission_asset="USDT", time=start + timedelta(seconds=seconds))
             for seconds, order_id, side, price, pnl in [(1, "entry", "BUY", "100", "0"), (120, "manual", "SELL", "102", "2"),
                                                         (130, "other", "BUY", "100", "0"), (140, "other-close", "SELL", "200", "100")]
             if not (scenario == "missing_close" and order_id == "manual")]
    if scenario == "partial_entry":
        # 同一张限价单在前一笔仓位归零的同一毫秒继续成交，后一笔平仓记录尚未同步。
        fills.insert(2, TradingFill(symbol="BTCUSDT", order_id="entry", side="BUY", qty=Decimal(1), price=Decimal(100),
                                    realized_pnl=Decimal(0), commission=Decimal("0.1"), commission_asset="USDT", time=start + timedelta(seconds=120)))
    class Session:
        async def scalars(self, query):
            return fills
    class Client:
        exchange = "bybit"
        async def trades(self, symbol, since_ms):
            assert since_ms <= int(start.timestamp() * 1000)
            return []
    async def save(*args):
        pass
    monkeypatch.setattr(trading, "save_fills", save)
    async def check():
        engine = trading.Executor()
        p = SimpleNamespace(symbol="BTCUSDT", first_fill_at=start + timedelta(seconds=90))
        entry = SimpleNamespace(purpose="entry", filled_qty=Decimal(2 if scenario == "partial_entry" else 1), exchange_id="entry", created_at=start)
        try:
            if scenario != "complete":
                with pytest.raises(trading.ExchangeError, match="完整的开平仓成交"):
                    await engine.realized(Session(), Client(), p, [entry])
            else:
                assert await engine.realized(Session(), Client(), p, [entry]) == {"realized_pnl": "2", "commission": "0.2", "net": "1.8"}
        finally:
            await engine.market.http.aclose()
    asyncio.run(check())
