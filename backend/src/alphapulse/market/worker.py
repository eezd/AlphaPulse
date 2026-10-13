"""行情进程：全历史补齐、断线/定时追平、币安 WS 实时推送。

用法：python -m alphapulse.market.worker
"""

import asyncio
import json
import logging
import time

import httpx
import websockets
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import redis
from alphapulse.market.binance import REST, WS, BinanceClient
from alphapulse.market.channels import kline_channel, ticker_channel
from alphapulse.models import Kline, KlineSyncState, MarketSymbol
from alphapulse.models.market import INTERVALS, MARKETS

logger = logging.getLogger("alphapulse.market.worker")

# 每个补齐任务连续拉取的页数，之后重新挑选优先级最高的任务（新加的交易对能插队补大周期）
PAGES_PER_TURN = 20
CATCHUP_SECONDS = 3600
SYMBOL_CHECK_SECONDS = 10
# miniTicker 约每秒推送，这么久没消息视为连接卡死
STALL_SECONDS = 60
ACTIVE_STATUSES = ("pending", "backfilling", "ready")

client = BinanceClient()
# WS（重新）连上后触发一次追平，补断线期间的缺口
catchup_needed = asyncio.Event()


def row_to_values(market: str, symbol: str, interval: str, row: list) -> dict:
    return {
        "market": market, "symbol": symbol, "interval": interval,
        "open_time": row[0], "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5],
        "close_time": row[6], "quote_volume": row[7], "trades": row[8], "taker_buy_base": row[9], "taker_buy_quote": row[10],
    }


async def upsert_klines(values: list[dict]) -> None:
    stmt = pg_insert(Kline).values(values)
    updatable = ("open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote")
    stmt = stmt.on_conflict_do_update(
        index_elements=[Kline.market, Kline.symbol, Kline.interval, Kline.open_time],
        set_={c: stmt.excluded[c] for c in updatable},
    )
    async with SessionLocal() as session:
        await session.execute(stmt)
        await session.commit()


async def sync_page(market: str, symbol: str, interval: str) -> bool:
    """从上次同步位置拉一页并落库，返回是否已追平到最新"""
    async with SessionLocal() as session:
        state = await session.get(KlineSyncState, (market, symbol, interval))
        if state is None:
            state = KlineSyncState(market=market, symbol=symbol, interval=interval)
            session.add(state)
        # 从最后一根（可能未收盘）重新拉起；首次从 0 开始，币安会返回上市后的第一根
        rows = await client.klines(market, symbol, interval, state.latest_open_time or 0)
        if rows:
            await upsert_klines([row_to_values(market, symbol, interval, r) for r in rows])
            state.earliest_open_time = state.earliest_open_time or rows[0][0]
            state.latest_open_time = rows[-1][0]
        caught_up = len(rows) < REST[market]["kline_limit"]
        if caught_up:
            state.backfill_done = True
        await session.commit()
    return caught_up


async def next_backfill_task(market: str) -> tuple[MarketSymbol, str] | None:
    async with SessionLocal() as session:
        symbols = (await session.scalars(
            select(MarketSymbol).where(MarketSymbol.market == market, MarketSymbol.status.in_(("pending", "backfilling")))
            .order_by(MarketSymbol.added_at)
        )).all()
        done = set((await session.execute(
            select(KlineSyncState.symbol, KlineSyncState.interval)
            .where(KlineSyncState.market == market, KlineSyncState.backfill_done)
        )).all())
    unfinished = []
    for s in symbols:
        if all((s.symbol, interval) in done for interval in INTERVALS):
            await set_symbol_status(s.id, "ready")
            logger.info("%s %s 全部周期补齐完成", market, s.symbol)
        else:
            unfinished.append(s)
    for interval in INTERVALS:
        for s in unfinished:
            if (s.symbol, interval) not in done:
                return s, interval
    return None


async def set_symbol_status(symbol_id: int, status: str, error: str | None = None) -> None:
    async with SessionLocal() as session:
        s = await session.get(MarketSymbol, symbol_id)
        if s is None:
            # 补齐过程中交易对被用户删除
            return
        s.status = status
        s.last_error = error
        await session.commit()


async def backfill_loop(market: str) -> None:
    while True:
        try:
            task = await next_backfill_task(market)
        except Exception:
            logger.exception("%s 选取补齐任务出错，稍后重试", market)
            await asyncio.sleep(10)
            continue
        if task is None:
            await asyncio.sleep(5)
            continue
        s, interval = task
        try:
            if s.status == "pending":
                await set_symbol_status(s.id, "backfilling")
                logger.info("%s %s 开始补齐", market, s.symbol)
            for _ in range(PAGES_PER_TURN):
                if await sync_page(market, s.symbol, interval):
                    logger.info("%s %s %s 补齐完成", market, s.symbol, interval)
                    break
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                # 交易对不存在或已下架：标记错误，不再重试
                await set_symbol_status(s.id, "error", e.response.text[:500])
                logger.error("%s %s 补齐失败：%s", market, s.symbol, e.response.text[:200])
            else:
                logger.exception("%s %s %s 补齐出错，稍后重试", market, s.symbol, interval)
                await asyncio.sleep(10)
        except Exception:
            logger.exception("%s %s %s 补齐出错，稍后重试", market, s.symbol, interval)
            await asyncio.sleep(10)


async def catchup_loop() -> None:
    """已补齐的周期从上次 REST 同步位置追平到最新：每小时一次，WS 重连后立即一次"""
    while True:
        try:
            await asyncio.wait_for(catchup_needed.wait(), timeout=CATCHUP_SECONDS)
        except TimeoutError:
            pass
        catchup_needed.clear()
        try:
            async with SessionLocal() as session:
                states = (await session.execute(
                    select(KlineSyncState.market, KlineSyncState.symbol, KlineSyncState.interval)
                    .join(MarketSymbol, (MarketSymbol.market == KlineSyncState.market) & (MarketSymbol.symbol == KlineSyncState.symbol))
                    .where(KlineSyncState.backfill_done, MarketSymbol.status.in_(ACTIVE_STATUSES))
                )).all()
        except Exception:
            logger.exception("读取追平列表失败，下次再试")
            continue
        started = time.monotonic()
        for market, symbol, interval in states:
            try:
                while not await sync_page(market, symbol, interval):
                    pass
            except Exception:
                logger.exception("%s %s %s 追平出错", market, symbol, interval)
        logger.info("追平完成：%d 个周期，用时 %.1fs", len(states), time.monotonic() - started)


async def active_symbols(market: str) -> list[str]:
    async with SessionLocal() as session:
        return list((await session.scalars(
            select(MarketSymbol.symbol).where(MarketSymbol.market == market, MarketSymbol.status.in_(ACTIVE_STATUSES))
            .order_by(MarketSymbol.symbol)
        )).all())


async def handle_message(market: str, raw: str | bytes) -> None:
    data = json.loads(raw)["data"]
    symbol = data["s"]
    if data["e"] == "kline":
        k = data["k"]
        payload = {"t": k["t"], "o": k["o"], "h": k["h"], "l": k["l"], "c": k["c"], "v": k["v"], "q": k["q"], "x": k["x"]}
        await redis.publish(kline_channel(market, symbol, k["i"]), json.dumps(payload))
        # 收盘 K 线入库；1s 只推送不落库
        if k["x"] and k["i"] != "1s":
            await upsert_klines([{
                "market": market, "symbol": symbol, "interval": k["i"],
                "open_time": k["t"], "open": k["o"], "high": k["h"], "low": k["l"], "close": k["c"], "volume": k["v"],
                "close_time": k["T"], "quote_volume": k["q"], "trades": k["n"], "taker_buy_base": k["V"], "taker_buy_quote": k["Q"],
            }])
    elif data["e"] == "24hrMiniTicker":
        payload = {"c": data["c"], "o": data["o"], "h": data["h"], "l": data["l"], "v": data["v"], "q": data["q"], "E": data["E"]}
        await redis.publish(ticker_channel(market, symbol), json.dumps(payload))


async def stream_loop(market: str) -> None:
    while True:
        try:
            symbols = await active_symbols(market)
            if not symbols:
                await asyncio.sleep(SYMBOL_CHECK_SECONDS)
                continue
            intervals = INTERVALS + (["1s"] if market == "spot" else [])
            streams = [f"{s.lower()}@kline_{i}" for s in symbols for i in intervals] + [f"{s.lower()}@miniTicker" for s in symbols]
            async with websockets.connect(f"{WS[market]}?streams={'/'.join(streams)}", max_size=2**22) as ws:
                logger.info("%s WS 已连接：%d 个交易对，%d 个流", market, len(symbols), len(streams))
                catchup_needed.set()
                next_check = time.monotonic() + SYMBOL_CHECK_SECONDS
                last_message = time.monotonic()
                while True:
                    # 带超时接收：没有消息时也要按时检查交易对变化和连接是否卡死
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=SYMBOL_CHECK_SECONDS)
                    except TimeoutError:
                        pass
                    else:
                        last_message = time.monotonic()
                        await handle_message(market, raw)
                    if time.monotonic() - last_message >= STALL_SECONDS:
                        logger.warning("%s WS %ds 未收到消息，重连", market, STALL_SECONDS)
                        break
                    if time.monotonic() >= next_check:
                        next_check = time.monotonic() + SYMBOL_CHECK_SECONDS
                        if await active_symbols(market) != symbols:
                            logger.info("%s 交易对列表变化，重新订阅", market)
                            break
        except Exception:
            logger.exception("%s WS 循环出错，5s 后重连", market)
            await asyncio.sleep(5)


async def main() -> None:
    # 各循环自行重试可恢复的错误；仍有异常逃出时整个进程退出，由 docker 重启
    async with asyncio.TaskGroup() as tg:
        for m in MARKETS:
            tg.create_task(backfill_loop(m))
            tg.create_task(stream_loop(m))
        tg.create_task(catchup_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx 每个请求一行 INFO，补齐时刷屏
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
