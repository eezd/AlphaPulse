"""由成交记录还原历史仓位。纯计算，不依赖数据库，便于测试"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol


class Fill(Protocol):
    """一笔成交；models.TradingFill 满足此接口"""

    symbol: str
    order_id: str
    side: str
    price: Decimal
    qty: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    time: datetime


@dataclass
class ClosedPosition:
    symbol: str
    # long / short
    direction: str
    opened_at: datetime
    closed_at: datetime | None = None
    # 开仓（含加仓）成交量合计，及开仓、平仓方向的成交额
    quantity: Decimal = Decimal(0)
    open_value: Decimal = Decimal(0)
    close_qty: Decimal = Decimal(0)
    close_value: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    # 只计 USDT 计价的手续费，与 Executor.realized 口径一致
    commission: Decimal = Decimal(0)
    # 参与这笔仓位的订单，用于关联程序的计划
    order_ids: set[str] = field(default_factory=set)
    # 结算计划时核对入场成交是否全部进入已平仓仓位，不能只凭订单 ID 判断。
    entry_quantities: dict[str, Decimal] = field(default_factory=dict)
    # 带符号的持仓量：多为正、空为负
    position: Decimal = Decimal(0)

    @property
    def entry_price(self) -> Decimal:
        return self.open_value / self.quantity

    @property
    def exit_price(self) -> Decimal:
        return self.close_value / self.close_qty

    @property
    def net(self) -> Decimal:
        return self.realized_pnl - self.commission


def build_positions(fills: list[Fill]) -> list[ClosedPosition]:
    """按币、按成交顺序累计带符号的持仓量（账户为单向持仓）：从 0 开始到回到 0 为一笔仓位。
    一笔成交跨过 0（反手）时拆成平仓和新开仓两段，realizedPnl 归平仓段，手续费按数量拆分。
    还没回到 0 的仓位不算历史。fills 须按 (symbol, time, trade_id) 排序"""
    closed: list[ClosedPosition] = []
    current: dict[str, ClosedPosition] = {}
    for fill in fills:
        fee = fill.commission if fill.commission_asset == "USDT" else Decimal(0)
        remaining = fill.qty if fill.side == "BUY" else -fill.qty
        while remaining:
            pos = current.get(fill.symbol)
            if pos is None:
                pos = current[fill.symbol] = ClosedPosition(fill.symbol, "long" if remaining > 0 else "short", fill.time)
            if pos.position == 0 or (pos.position > 0) == (remaining > 0):
                part = remaining
                pos.quantity += abs(part)
                pos.open_value += abs(part) * fill.price
                pos.entry_quantities[fill.order_id] = pos.entry_quantities.get(fill.order_id, Decimal(0)) + abs(part)
            else:
                part = remaining if abs(remaining) <= abs(pos.position) else -pos.position
                pos.close_qty += abs(part)
                pos.close_value += abs(part) * fill.price
                pos.realized_pnl += fill.realized_pnl
            pos.position += part
            pos.commission += fee * abs(part) / fill.qty
            pos.order_ids.add(fill.order_id)
            remaining -= part
            if pos.position == 0:
                pos.closed_at = fill.time
                closed.append(current.pop(fill.symbol))
    return closed
