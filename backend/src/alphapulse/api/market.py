import asyncio
import json
import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.core.auth import require_admin, websocket_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.market.binance import BinanceClient
from alphapulse.market.channels import kline_channel, ticker_channel
from alphapulse.models import Kline, KlineSyncState, MarketSymbol
from alphapulse.models.market import INTERVALS

router = APIRouter(prefix="/api/market", tags=["market"], dependencies=[Depends(require_admin)])
# WebSocket 握手不能用基于 Request 的依赖，单独鉴权
ws_router = APIRouter(prefix="/api/market", tags=["market"])

Market = Literal["spot", "um"]
SYMBOLS_CACHE_SECONDS = 3600
MAX_KLINES = 1500

client = BinanceClient()


class IntervalProgress(BaseModel):
    done: bool
    percent: float


class SymbolOut(BaseModel):
    id: int
    market: str
    symbol: str
    base: str
    quote: str
    price_precision: int
    volume_precision: int
    status: str
    is_default: bool
    added_at: datetime
    last_error: str | None
    progress: dict[str, IntervalProgress]


class SymbolIn(BaseModel):
    market: Market
    symbol: str


class TradableSymbol(BaseModel):
    symbol: str
    base: str
    quote: str
    price_precision: int
    volume_precision: int


async def tradable_symbols(market: str) -> list[dict]:
    key = f"{KEY_PREFIX}market:symbols:{market}"
    if cached := await redis.get(key):
        return json.loads(cached)
    symbols = await client.tradable_symbols(market)
    await redis.set(key, json.dumps(symbols), ex=SYMBOLS_CACHE_SECONDS)
    return symbols


@router.get("/symbols")
async def list_symbols(session: Annotated[AsyncSession, Depends(get_session)]) -> list[SymbolOut]:
    symbols = (await session.scalars(select(MarketSymbol).order_by(MarketSymbol.is_default.desc(), MarketSymbol.added_at))).all()
    states = {(s.market, s.symbol, s.interval): s for s in await session.scalars(select(KlineSyncState))}
    now = int(time.time() * 1000)

    def progress(s: MarketSymbol, interval: str) -> IntervalProgress:
        state = states.get((s.market, s.symbol, interval))
        if state is None or state.earliest_open_time is None:
            return IntervalProgress(done=False, percent=0)
        if state.backfill_done:
            return IntervalProgress(done=True, percent=100)
        span = max(now - state.earliest_open_time, 1)
        return IntervalProgress(done=False, percent=round((state.latest_open_time - state.earliest_open_time) / span * 100, 1))

    return [
        SymbolOut.model_validate({**s.__dict__, "progress": {i: progress(s, i) for i in INTERVALS}})
        for s in symbols
    ]


@router.post("/symbols", status_code=status.HTTP_201_CREATED)
async def add_symbol(body: SymbolIn, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    symbol = body.symbol.strip().upper()
    match = next((s for s in await tradable_symbols(body.market) if s["symbol"] == symbol), None)
    if match is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{body.market} 市场没有可交易的 {symbol}")
    session.add(MarketSymbol(market=body.market, **match))
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{symbol} 已存在") from None


@router.delete("/symbols/{symbol_id}")
async def delete_symbol(symbol_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """删除交易对及其 K 线（K 线可随时从币安重新补齐）"""
    s = await session.get(MarketSymbol, symbol_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "交易对不存在")
    await session.execute(delete(Kline).where(Kline.market == s.market, Kline.symbol == s.symbol))
    await session.execute(delete(KlineSyncState).where(KlineSyncState.market == s.market, KlineSyncState.symbol == s.symbol))
    await session.delete(s)
    await session.commit()


@router.get("/exchange-info")
async def exchange_info(market: Market) -> list[TradableSymbol]:
    return [TradableSymbol.model_validate(s) for s in await tradable_symbols(market)]


@router.get("/klines")
async def klines(
    session: Annotated[AsyncSession, Depends(get_session)],
    market: Market,
    symbol: str,
    interval: str,
    end: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_KLINES)] = 500,
) -> list[list[float]]:
    """返回 open_time < end 的最近 limit 根，按时间正序：[open_time, open, high, low, close, volume, quote_volume]"""
    if interval == "1s":
        # 1s 不落库，直接取币安最近数据
        if market != "spot":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "合约没有 1s 周期")
        params = {"symbol": symbol, "interval": "1s", "limit": limit, **({"endTime": end - 1} if end else {})}
        rows = await client.get("spot", "klines", params)
        return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7])] for r in rows]
    if interval not in INTERVALS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的周期：{interval}")
    stmt = (
        select(Kline.open_time, Kline.open, Kline.high, Kline.low, Kline.close, Kline.volume, Kline.quote_volume)
        .where(Kline.market == market, Kline.symbol == symbol, Kline.interval == interval)
        .order_by(Kline.open_time.desc())
        .limit(limit)
    )
    if end:
        stmt = stmt.where(Kline.open_time < end)
    rows = (await session.execute(stmt)).all()
    return [[r[0], *(float(v) for v in r[1:])] for r in reversed(rows)]


@router.get("/ticker")
async def ticker(market: Market, symbol: str) -> dict:
    return await client.ticker(market, symbol)


@ws_router.websocket("/ws")
async def market_ws(websocket: WebSocket) -> None:
    """客户端发送 {"market","symbol","interval"} 切换订阅；服务端推送 {"type":"kline"|"ticker", market, symbol, interval?, data}"""
    if websocket_admin(websocket) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    pubsub = redis.pubsub()
    subscribed = asyncio.Event()

    async def forward() -> None:
        await subscribed.wait()
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                continue
            # 频道格式：ap:pub:kline:<market>:<symbol>:<interval> 或 ap:pub:ticker:<market>:<symbol>
            parts = message["channel"].split(":")
            await websocket.send_text(json.dumps({
                "type": parts[2],
                "market": parts[3],
                "symbol": parts[4],
                "interval": parts[5] if parts[2] == "kline" else None,
                "data": json.loads(message["data"]),
            }))

    task = asyncio.create_task(forward())
    try:
        while True:
            sub = await websocket.receive_json()
            if subscribed.is_set():
                await pubsub.unsubscribe()
            await pubsub.subscribe(
                kline_channel(sub["market"], sub["symbol"], sub["interval"]),
                ticker_channel(sub["market"], sub["symbol"]),
            )
            subscribed.set()
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        await pubsub.aclose()
