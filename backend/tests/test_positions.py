from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alphapulse.trading.positions import build_positions

T0 = datetime(2026, 10, 15, 12, tzinfo=UTC)


@dataclass
class Fill:
    symbol: str
    order_id: int
    side: str
    price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    time: datetime


def fill(trade_id: int, side: str, qty: str, price: str, pnl: str = "0", fee: str = "0.1", asset: str = "USDT",
         symbol: str = "BTCUSDT", order_id: int | None = None) -> Fill:
    return Fill(
        symbol=symbol, order_id=order_id or trade_id, side=side, qty=Decimal(qty), price=Decimal(price),
        realized_pnl=Decimal(pnl), commission=Decimal(fee), commission_asset=asset, time=T0 + timedelta(seconds=trade_id),
    )


def test_open_and_close_once():
    [p] = build_positions([fill(1, "BUY", "0.01", "80000"), fill(2, "SELL", "0.01", "80500", pnl="5")])
    assert (p.direction, p.quantity, p.entry_price, p.exit_price) == ("long", Decimal("0.01"), 80000, 80500)
    assert (p.realized_pnl, p.commission, p.net) == (5, Decimal("0.2"), Decimal("4.8"))
    assert (p.opened_at, p.closed_at) == (T0 + timedelta(seconds=1), T0 + timedelta(seconds=2))


def test_partial_closes_average_exit_price():
    # #40：0.0126 多单分三档止盈
    [p] = build_positions([
        fill(1, "BUY", "0.0126", "80790"),
        fill(2, "SELL", "0.0075", "81193.9", pnl="3.02925"),
        fill(3, "SELL", "0.0037", "81395.9", pnl="2.24183"),
        fill(4, "SELL", "0.0014", "81597.9", pnl="1.13106"),
    ])
    assert p.quantity == Decimal("0.0126")
    assert round(p.exit_price, 2) == Decimal("81298.11")
    assert p.realized_pnl == Decimal("6.40214")


def test_flip_through_zero_splits_positions():
    # 空 0.01 后一笔买 0.03：先平空 0.01，再开多 0.02；手续费按数量拆分
    first, second = build_positions([
        fill(1, "SELL", "0.01", "80000"),
        fill(2, "BUY", "0.03", "79000", pnl="10", fee="0.3"),
        fill(3, "SELL", "0.02", "79500", pnl="10"),
    ])
    assert (first.direction, first.quantity, first.exit_price, first.realized_pnl) == ("short", Decimal("0.01"), 79000, 10)
    assert first.commission == Decimal("0.2")
    assert (second.direction, second.quantity, second.entry_price) == ("long", Decimal("0.02"), 79000)
    assert second.commission == Decimal("0.3")
    assert second.opened_at == first.closed_at


def test_open_position_is_not_history_and_symbols_are_independent():
    positions = build_positions([
        fill(1, "BUY", "1", "2400", symbol="ETHUSDT"),
        fill(2, "BUY", "0.01", "80000"),
        fill(3, "SELL", "0.01", "80100", pnl="1"),
    ])
    assert [p.symbol for p in positions] == ["BTCUSDT"]


def test_non_usdt_commission_is_ignored():
    [p] = build_positions([fill(1, "BUY", "0.01", "80000", asset="BNB"), fill(2, "SELL", "0.01", "80000")])
    assert p.commission == Decimal("0.1")
