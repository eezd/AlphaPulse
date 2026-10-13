import asyncio
import json
import logging
import time
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.websockets import WebSocketState
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.core.auth import require_admin, websocket_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.market.binance import BinanceClient, RateLimited
from alphapulse.market.channels import kline_channel, ticker_channel
from alphapulse.models import Kline, KlineSyncState, MarketSymbol
from alphapulse.models.market import INTERVALS

router = APIRouter(prefix="/api/market", tags=["market"], dependencies=[Depends(require_admin)])
# WebSocket 握手不能用基于 Request 的依赖，单独鉴权
ws_router = APIRouter(prefix="/api/market", tags=["market"])

Market = Literal["spot", "um"]
SYMBOLS_CACHE_SECONDS = 3600
MAX_KLINES = 1500

logger = logging.getLogger("alphapulse.api.market")
# API 请求不等待限流，直接报 503，避免 HTTP 请求被挂起
client = BinanceClient(wait=False)


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


class Subscription(BaseModel):
    market: Market
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,30}$")
    interval: str

    @field_validator("interval")
    @classmethod
    def check_interval(cls, value: str) -> str:
        if value not in (*INTERVALS, "1s"):
            raise ValueError(f"不支持的周期：{value}")
        return value


async def call_binance[T](request: Awaitable[T]) -> T:
    """把币安调用的失败转换成明确的 HTTP 错误，而不是 500"""
    try:
        return await request
    except RateLimited as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from None
    except httpx.HTTPStatusError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"币安返回 {e.response.status_code}：{e.response.text[:200]}") from None
    except httpx.TransportError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"无法连接币安：{e!r}") from None


async def ensure_symbol(session: AsyncSession, market: str, symbol: str) -> None:
    """只允许查询已添加的交易对，不把任意 symbol 转发给币安"""
    exists = await session.scalar(select(MarketSymbol.id).where(MarketSymbol.market == market, MarketSymbol.symbol == symbol))
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未添加交易对 {market} {symbol}")


async def tradable_symbols(market: str) -> list[dict]:
    key = f"{KEY_PREFIX}market:symbols:{market}"
    if cached := await redis.get(key):
        return json.loads(cached)
    symbols = await call_binance(client.tradable_symbols(market))
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
        await ensure_symbol(session, market, symbol)
        params = {"symbol": symbol, "interval": "1s", "limit": limit, **({"endTime": end - 1} if end else {})}
        rows = await call_binance(client.get("spot", "klines", params))
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
async def ticker(session: Annotated[AsyncSession, Depends(get_session)], market: Market, symbol: str) -> dict:
    await ensure_symbol(session, market, symbol)
    return await call_binance(client.ticker(market, symbol))


@ws_router.websocket("/ws")
async def market_ws(websocket: WebSocket) -> None:
    """客户端发送 {"market","symbol","interval"} 切换订阅；服务端推送 {"type":"kline"|"ticker", market, symbol, interval?, data}"""
    if websocket_admin(websocket) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    pubsub = redis.pubsub()
    subscribed = asyncio.Event()

    async def receive() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                sub = Subscription.model_validate_json(raw)
            except ValidationError as e:
                await websocket.send_text(json.dumps({"type": "error", "message": f"订阅参数无效：{e.errors()[0]['msg']}"}, ensure_ascii=False))
                continue
            if subscribed.is_set():
                await pubsub.unsubscribe()
            await pubsub.subscribe(kline_channel(sub.market, sub.symbol, sub.interval), ticker_channel(sub.market, sub.symbol))
            subscribed.set()

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

    # 收、发任一方向结束（客户端断开或出错）就整体收尾；非正常断开时主动关闭，让前端重连，
    # 避免转发任务已死而连接看起来仍正常
    tasks = [asyncio.create_task(receive()), asyncio.create_task(forward())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                logger.warning("行情 WS 连接异常结束：%r", error)
    finally:
        for task in tasks:
            task.cancel()
        await pubsub.aclose()
        if websocket.application_state == WebSocketState.CONNECTED and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011)
