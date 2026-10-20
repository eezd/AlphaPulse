"""账户、持仓、挂单快照：定时从模拟盘同步并写入 Redis，供页面和分析器读取"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import TradingAccount, TradingCredential, TradingSymbol
from alphapulse.trading.client import DemoClient, ExchangeError, create_client, credential_key

logger = logging.getLogger("alphapulse.trading.snapshot")

# 进程挂掉后快照自动过期，页面据此显示「引擎离线」
STATUS_KEY = f"{KEY_PREFIX}trader:status"
STATUS_TTL_SECONDS = 15
SNAPSHOT_SECONDS = 3
SNAPSHOT_FRESH_SECONDS = 30
# 杠杆和保证金模式很少变，按币缓存
SYMBOL_CONFIG_SECONDS = 60


class SnapshotError(Exception):
    pass


def require_fresh_snapshot(snapshot: dict | None) -> dict:
    """页面可以显示旧数据，但交易决策必须使用最近一次成功同步的账户。"""
    if snapshot is None or snapshot["error"] or not snapshot["synced_at"] or not snapshot["account"]:
        raise SnapshotError("取不到可用的模拟盘账户快照（引擎离线或同步失败）")
    synced_at = datetime.fromisoformat(snapshot["synced_at"])
    if (datetime.now(UTC) - synced_at).total_seconds() > SNAPSHOT_FRESH_SECONDS:
        raise SnapshotError("模拟盘账户快照已过期")
    return snapshot


class Snapshot:
    def __init__(self) -> None:
        self.client: DemoClient | None = None
        self.key_ciphertext: tuple | None = None
        # 最近一次成功同步的数据；同步失败时保留并标注错误，避免页面闪空
        self.data: dict | None = None
        self.synced_at: datetime | None = None
        self.symbol_config: dict[str, tuple[float, dict]] = {}

    async def ensure_client(self, account: TradingCredential | None) -> DemoClient | None:
        ciphertext = credential_key(account)
        if ciphertext != self.key_ciphertext:
            # key 被修改或删除：旧连接作废，旧数据属于另一个账户，一并清掉
            if self.client is not None:
                await self.client.http.aclose()
            self.client = None
            self.data = self.synced_at = None
            self.symbol_config.clear()
            self.key_ciphertext = ciphertext
            if account is not None and ciphertext is not None:
                self.client = create_client(account)
        return self.client

    async def leverage_and_margin(self, client: DemoClient, symbol: str) -> dict:
        cached = self.symbol_config.get(symbol)
        if cached is None or time.monotonic() - cached[0] > SYMBOL_CONFIG_SECONDS:
            config = await client.symbol_config(symbol)
            cached = (time.monotonic(), {"leverage": config["leverage"], "margin_type": config["marginType"]})
            self.symbol_config[symbol] = cached
        return cached[1]

    async def fetch(self, client: DemoClient, symbols: list[str]) -> dict:
        account = await client.account()
        positions = [p for p in await client.positions() if float(p["positionAmt"]) != 0]
        # 挂单只查交易列表里的币和有持仓的币：不带 symbol 查全部的权重是 40，3 秒一次太贵
        watched = sorted(set(symbols) | {p["symbol"] for p in positions})
        orders, algo_orders = [], []
        for symbol in watched:
            orders += await client.open_orders(symbol)
            algo_orders += await client.open_orders(symbol, True)
        return {
            "account": {
                "wallet_balance": account["totalWalletBalance"],
                "margin_balance": account["totalMarginBalance"],
                "available_balance": account["availableBalance"],
                "unrealized_pnl": account["totalUnrealizedProfit"],
            },
            "positions": [
                {
                    "symbol": p["symbol"],
                    "side": "LONG" if float(p["positionAmt"]) > 0 else "SHORT",
                    "amount": p["positionAmt"].lstrip("-"),
                    "entry_price": p["entryPrice"],
                    "break_even_price": p["breakEvenPrice"],
                    "mark_price": p["markPrice"],
                    "unrealized_pnl": p["unRealizedProfit"],
                    "liquidation_price": p["liquidationPrice"],
                    "notional": p["notional"].lstrip("-"),
                    "margin": p["isolatedMargin"] if float(p["isolatedMargin"]) else p["initialMargin"],
                    **await self.leverage_and_margin(client, p["symbol"]),
                }
                for p in positions
            ],
            "orders": [
                {
                    "symbol": o["symbol"], "client_id": o["clientOrderId"], "side": o["side"], "type": o["type"],
                    "price": o["price"], "quantity": o["origQty"], "filled": o["executedQty"], "reduce_only": o["reduceOnly"],
                    "time": o["time"],
                }
                for o in orders
            ],
            "algo_orders": [
                {
                    "symbol": o["symbol"], "client_id": o["clientAlgoId"], "side": o["side"], "type": o["orderType"],
                    "trigger_price": o["triggerPrice"], "quantity": o["quantity"], "close_position": o["closePosition"],
                    "reduce_only": o["reduceOnly"], "time": o["createTime"],
                }
                for o in algo_orders
            ],
        }

    async def publish(self) -> None:
        async with SessionLocal() as session:
            account = await session.get(TradingAccount, 1)
            credential = await session.get(TradingCredential, account.exchange) if account else None
            symbols = list((await session.scalars(select(TradingSymbol.symbol))).all())
        error = None
        client = await self.ensure_client(credential)
        if client is None:
            error = "还没有填写模拟盘 key"
        else:
            try:
                self.data = await self.fetch(client, symbols)
                self.synced_at = datetime.now(UTC)
            except ExchangeError as e:
                logger.warning("同步模拟盘失败：%s", e)
                error = str(e)
        status = {
            "exchange": account.exchange if account else "binance",
            "credential_updated_at": credential.updated_at.isoformat() if credential else None,
            "updated_at": datetime.now(UTC).isoformat(),
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
            "error": error,
            **(self.data or {"account": None, "positions": [], "orders": [], "algo_orders": []}),
        }
        await redis.set(STATUS_KEY, json.dumps(status), ex=STATUS_TTL_SECONDS)


async def snapshot_loop() -> None:
    snapshot = Snapshot()
    while True:
        try:
            await snapshot.publish()
        except Exception:
            logger.exception("发布快照失败，稍后重试")
        await asyncio.sleep(SNAPSHOT_SECONDS)




async def read_snapshot() -> dict | None:
    raw = await redis.get(STATUS_KEY)
    if not raw:
        return None
    snapshot = json.loads(raw)
    async with SessionLocal() as session:
        account = await session.get(TradingAccount, 1)
        exchange = account.exchange if account else "binance"
        credential = await session.get(TradingCredential, exchange)
    if snapshot.get("exchange") != exchange or snapshot.get("credential_updated_at") != (credential.updated_at.isoformat() if credential else None):
        return None
    return snapshot
