"""模拟盘页：引擎状态、启停、交易币种、时间线。账户和持仓来自 trader 进程写入 Redis 的快照"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.ai import llm
from alphapulse.api.crawler import LOCAL_TZ
from alphapulse.api.market import call_binance, client as market_client
from alphapulse.core.auth import require_admin
from alphapulse.core.db import get_session
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.models import AiProvider, TradingAccount, TradingAnalysis, TradingEvent, TradingFill, TradingOrder, TradingSymbol
from alphapulse.trading.client import OWN_CLIENT_ID, ExchangeError, public_get
from alphapulse.trading.executor import MANUAL_ACTION_KEY
from alphapulse.trading.positions import build_positions
from alphapulse.trading.plan import DAILY_ANALYSIS_LIMIT
from alphapulse.trading.snapshot import read_snapshot

router = APIRouter(prefix="/api/trading", tags=["trading"], dependencies=[Depends(require_admin)])

SYMBOLS_CACHE_KEY = f"{KEY_PREFIX}trading:symbols"
SYMBOLS_CACHE_SECONDS = 3600
# 删除前要求快照足够新，否则无法确认没有仓位和挂单
SNAPSHOT_FRESH_SECONDS = 30
# 平仓、清除挂单的请求等执行器取走的时限：引擎没及时处理就作废，不在之后意外执行
MANUAL_ACTION_TTL_SECONDS = 30
# 执行器正在管理的计划状态
LIVE_PLAN_STATUSES = ("pending", "open", "closing")


class TodayUsage(BaseModel):
    analyses: int
    limit: int
    input_tokens: int
    output_tokens: int


class StatusOut(BaseModel):
    configured: bool
    provider_id: int | None
    reasoning_effort: str | None
    # trader 进程的心跳快照；进程离线时为空
    snapshot: dict | None
    today: TodayUsage


class ConfigIn(BaseModel):
    provider_id: int | None = None
    reasoning_effort: llm.Effort | None = None


class SymbolOption(BaseModel):
    symbol: str
    base: str


class SymbolOut(BaseModel):
    symbol: str
    added_at: datetime
    analysis_requested_at: datetime | None
    analyzing: bool
    running: bool
    running_changed_at: datetime | None
    capital_usdt: Decimal | None
    # 实际用于仓位计算的资金：min(交易资金, 账户权益)；没有快照时无法计算
    effective_capital: Decimal | None
    # 当前计划：pending / open / closing，没有计划为空
    plan_id: int | None
    plan_status: str | None
    close_deadline: datetime | None


class SymbolConfigIn(BaseModel):
    capital_usdt: Annotated[Decimal, Field(gt=0, decimal_places=2)] | None


class SymbolIn(BaseModel):
    symbol: str


class AnalysisOut(BaseModel):
    id: int
    symbol: str
    reasons: list[str]
    dry_run: bool
    status: str
    action: str | None
    decision: dict | None
    sizing: dict | None
    error: str | None
    model: str | None
    reasoning_effort: str | None
    usage: dict | None
    started_at: datetime
    finished_at: datetime | None
    plan_status: str | None
    valid_until: datetime | None
    first_fill_at: datetime | None
    close_deadline: datetime | None
    closed_at: datetime | None
    close_reason: str | None
    result: dict | None


class AnalysisDetail(AnalysisOut):
    text: str | None
    transcript: list | None


class OrderOut(BaseModel):
    id: int
    purpose: str
    leg: int | None
    side: str
    type: str
    price: Decimal | None
    trigger_price: Decimal | None
    quantity: Decimal
    status: str
    filled_qty: Decimal
    avg_price: Decimal | None
    error: str | None
    created_at: datetime


class SymbolPlanOut(BaseModel):
    # 执行器正在管理的计划；没有时为最近一次待采纳的提议
    plan: AnalysisOut | None
    orders: list[OrderOut]


class EventOut(BaseModel):
    id: int
    created_at: datetime
    symbol: str | None
    analysis_id: int | None
    kind: str
    level: str
    message: str
    data: dict | None


class PositionHistoryItem(BaseModel):
    symbol: str
    direction: str
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    commission: Decimal
    net: Decimal
    # program：成交里有程序计划的订单；manual：在币安网页等程序外开的仓
    source: str
    analysis_id: int | None
    close_reason: str | None


class PositionSummary(BaseModel):
    count: int
    wins: int
    net: Decimal
    commission: Decimal


class PositionHistoryPage(BaseModel):
    # 按筛选条件统计全部仓位，不只是当前页
    summary: PositionSummary
    total: int
    items: list[PositionHistoryItem]


async def get_account(session: AsyncSession) -> TradingAccount:
    return await session.get(TradingAccount, 1) or TradingAccount(id=1)


@router.get("/status")
async def get_status(session: Annotated[AsyncSession, Depends(get_session)]) -> StatusOut:
    account = await get_account(session)
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    count, input_tokens, output_tokens = (await session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(TradingAnalysis.usage["input_tokens"].as_integer()), 0),
            func.coalesce(func.sum(TradingAnalysis.usage["output_tokens"].as_integer()), 0),
        ).where(TradingAnalysis.started_at >= today)
    )).one()
    return StatusOut(
        configured=account.api_key_encrypted is not None, provider_id=account.provider_id, reasoning_effort=account.reasoning_effort,
        snapshot=await read_snapshot(),
        today=TodayUsage(analyses=count, limit=DAILY_ANALYSIS_LIMIT, input_tokens=input_tokens, output_tokens=output_tokens),
    )


@router.patch("/config")
async def patch_config(body: ConfigIn, session: Annotated[AsyncSession, Depends(get_session)]) -> StatusOut:
    account = await get_account(session)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("provider_id") is not None and await session.get(AiProvider, fields["provider_id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型配置不存在")
    for key, value in fields.items():
        setattr(account, key, value)
    session.add(account)
    await session.commit()
    return await get_status(session)


async def symbol_options() -> list[dict]:
    """可交易的币：模拟盘和实盘都在交易的 USDT 永续，只含加密货币（排除美股、商品等 TradFi）"""
    if cached := await redis.get(SYMBOLS_CACHE_KEY):
        return json.loads(cached)

    def crypto_perpetuals(info: dict) -> dict[str, str]:
        return {
            s["symbol"]: s["baseAsset"] for s in info["symbols"]
            if s["contractType"] == "PERPETUAL" and s.get("underlyingType") == "COIN"
            and s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
        }

    try:
        demo = crypto_perpetuals(await public_get("/fapi/v1/exchangeInfo"))
    except ExchangeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    # 分析用实盘行情，执行在模拟盘，两边都要有
    mainnet = crypto_perpetuals(await call_binance(market_client.get("um", "exchange_info")))
    options = [{"symbol": s, "base": b} for s, b in sorted(demo.items()) if s in mainnet]
    await redis.set(SYMBOLS_CACHE_KEY, json.dumps(options), ex=SYMBOLS_CACHE_SECONDS)
    return options


@router.get("/symbol-options")
async def list_symbol_options() -> list[SymbolOption]:
    return [SymbolOption.model_validate(o) for o in await symbol_options()]


async def current_equity() -> Decimal | None:
    """账户权益，取自 trader 的快照；进程离线或未同步到币安时为空"""
    snapshot = await read_snapshot()
    return Decimal(snapshot["account"]["margin_balance"]) if snapshot and snapshot["account"] else None


async def get_symbol(session: AsyncSession, symbol: str) -> TradingSymbol:
    s = await session.get(TradingSymbol, symbol)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} 不在交易列表中")
    return s


async def set_running(session: AsyncSession, s: TradingSymbol, running: bool) -> None:
    if running and (await get_account(session)).api_key_encrypted is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "还没有填写模拟盘 key，请先到环境变量页填写")
    if running and s.capital_usdt is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{s.symbol} 还没有设置交易资金")
    if s.running != running:
        s.running = running
        s.running_changed_at = datetime.now(LOCAL_TZ)
        session.add(TradingEvent(
            symbol=s.symbol, kind="started" if running else "stopped",
            message="开始行情交易" if running else "停止行情交易：不再分析，撤销未成交的入场单；已有持仓的止损、止盈等程序规则继续执行",
        ))
        await session.commit()


@router.post("/symbols/{symbol}/start")
async def start(symbol: str, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    await set_running(session, await get_symbol(session, symbol), True)


@router.post("/symbols/{symbol}/stop")
async def stop(symbol: str, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    await set_running(session, await get_symbol(session, symbol), False)


@router.patch("/symbols/{symbol}")
async def patch_symbol(symbol: str, body: SymbolConfigIn, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """设置该币的交易资金：各币合计不超过账户权益（取不到权益时不校验）"""
    s = await get_symbol(session, symbol)
    capital = body.capital_usdt
    if capital == s.capital_usdt:
        return
    if capital is None and s.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "交易中不能清空交易资金")
    equity = await current_equity()
    if capital is not None and equity:
        others = await session.scalar(
            select(func.coalesce(func.sum(TradingSymbol.capital_usdt), 0)).where(TradingSymbol.symbol != symbol)
        )
        if capital + others > equity:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"各币交易资金合计不能超过账户权益 {equity:.2f} USDT（其他币已分配 {others:.2f}，最多还能设 {max(equity - others, 0):.2f}）")
    s.capital_usdt = capital
    session.add(TradingEvent(symbol=symbol, kind="capital_changed",
                             message=f"交易资金设为 {capital} USDT" if capital is not None else "清空交易资金"))
    await session.commit()


@router.get("/symbols")
async def list_symbols(session: Annotated[AsyncSession, Depends(get_session)]) -> list[SymbolOut]:
    symbols = (await session.scalars(select(TradingSymbol).order_by(TradingSymbol.added_at))).all()
    equity = await current_equity()
    analyzing = set((await session.scalars(select(TradingAnalysis.symbol).where(TradingAnalysis.status == "running"))).all())
    plans = {p.symbol: p for p in await session.scalars(
        select(TradingAnalysis).where(TradingAnalysis.plan_status.in_(LIVE_PLAN_STATUSES)).order_by(TradingAnalysis.id)
    )}
    return [
        SymbolOut(
            symbol=s.symbol, added_at=s.added_at, analysis_requested_at=s.analysis_requested_at,
            analyzing=s.symbol in analyzing, running=s.running, running_changed_at=s.running_changed_at, capital_usdt=s.capital_usdt,
            effective_capital=min(s.capital_usdt, equity) if s.capital_usdt and equity else None,
            plan_id=plans[s.symbol].id if s.symbol in plans else None,
            plan_status=plans[s.symbol].plan_status if s.symbol in plans else None,
            close_deadline=plans[s.symbol].close_deadline if s.symbol in plans else None,
        )
        for s in symbols
    ]


@router.post("/symbols", status_code=status.HTTP_201_CREATED)
async def add_symbol(body: SymbolIn, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    symbol = body.symbol.strip().upper()
    if not any(o["symbol"] == symbol for o in await symbol_options()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{symbol} 不是模拟盘和实盘都在交易的加密货币 USDT 永续合约")
    session.add(TradingSymbol(symbol=symbol))
    session.add(TradingEvent(symbol=symbol, kind="symbol_added", message="加入交易列表"))
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{symbol} 已在交易列表中") from None


async def fresh_snapshot(symbol: str, action: str) -> dict:
    """引擎在线且刚和币安同步过的快照"""
    snapshot = await read_snapshot()
    synced_at = snapshot and snapshot["synced_at"] and datetime.fromisoformat(snapshot["synced_at"])
    if not synced_at or datetime.now(LOCAL_TZ) - synced_at > timedelta(seconds=SNAPSHOT_FRESH_SECONDS):
        raise HTTPException(status.HTTP_409_CONFLICT, f"无法确认 {symbol} 的仓位和挂单（引擎离线或连接币安失败），暂时不能{action}")
    return snapshot


async def ensure_idle(session: AsyncSession, symbol: str, action: str) -> TradingSymbol:
    """只有空仓、没有本程序的挂单、没有进行中的计划和分析时，才能删除"""
    s = await session.get(TradingSymbol, symbol)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} 不在交易列表中")
    busy = await session.scalar(select(func.count()).where(
        TradingAnalysis.symbol == symbol,
        or_(TradingAnalysis.status == "running", TradingAnalysis.plan_status.in_(("proposed", *LIVE_PLAN_STATUSES))),
    ))
    if busy:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{symbol} 有进行中的分析或计划，不能{action}")
    snapshot = await fresh_snapshot(symbol, action)
    if any(p["symbol"] == symbol for p in snapshot["positions"]):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{symbol} 有持仓，不能{action}")
    if any(o["symbol"] == symbol and OWN_CLIENT_ID.match(o["client_id"]) for o in snapshot["orders"] + snapshot["algo_orders"]):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{symbol} 有本程序的挂单，不能{action}")
    return s


@router.delete("/symbols/{symbol}")
async def delete_symbol(symbol: str, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """只移出列表；分析、订单流水和时间线保留"""
    s = await ensure_idle(session, symbol, "删除")
    await session.delete(s)
    session.add(TradingEvent(symbol=symbol, kind="symbol_removed", message="移出交易列表"))
    await session.commit()


@router.post("/symbols/{symbol}/manual/{action}", status_code=status.HTTP_202_ACCEPTED)
async def manual_action(symbol: str, action: Literal["close", "cancel"], session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """一键平仓（同时停止该币的行情交易）、清除挂单：交给执行器执行，结果写入时间线"""
    label = "平仓" if action == "close" else "清除挂单"
    s = await get_symbol(session, symbol)
    await fresh_snapshot(symbol, label)
    if action == "close":
        # 不停止的话，平仓后会重新分析，可能马上又开仓
        await set_running(session, s, False)
    await redis.set(MANUAL_ACTION_KEY.format(symbol), action, ex=MANUAL_ACTION_TTL_SECONDS)


@router.get("/events")
async def list_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    symbol: str | None = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EventOut]:
    """按 id 倒序分页；指定 symbol 时同时返回账户级事件（启停等）"""
    query = select(TradingEvent).order_by(TradingEvent.id.desc()).limit(limit)
    if symbol:
        query = query.where(or_(TradingEvent.symbol == symbol, TradingEvent.symbol.is_(None)))
    if before:
        query = query.where(TradingEvent.id < before)
    return [EventOut.model_validate(e, from_attributes=True) for e in await session.scalars(query)]


@router.get("/positions/history")
async def list_position_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    outcome: Literal["profit", "loss"] | None = None,
    source: Literal["program", "manual"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PositionHistoryPage:
    """由币安成交记录还原的已平仓仓位，按平仓时间倒序；start/end 按平仓时间筛选。
    个人账户成交量小，每次全量还原后在内存里筛选、统计、分页"""
    fills = list(await session.scalars(select(TradingFill).order_by(TradingFill.symbol, TradingFill.time, TradingFill.trade_id)))
    # 程序下的普通单记录了 orderId；条件单（止损）触发后的订单号没有记录，靠同一仓位里的其他订单关联
    order_plans = dict((await session.execute(
        select(TradingOrder.exchange_id, TradingOrder.analysis_id)
        .where(TradingOrder.is_algo.is_(False), TradingOrder.exchange_id.is_not(None), TradingOrder.analysis_id.is_not(None))
    )).tuples().all())
    plans = {a.id: a for a in await session.scalars(
        select(TradingAnalysis).where(TradingAnalysis.id.in_(set(order_plans.values())))
    )}
    items = []
    for position in build_positions(fills):
        linked = [plans[order_plans[str(o)]] for o in position.order_ids if str(o) in order_plans]
        # 早期替换计划时持仓会交给新计划接管，优先取最终平仓的那个计划
        plan = max(linked, key=lambda a: (a.plan_status == "closed", a.id)) if linked else None
        items.append(PositionHistoryItem(
            symbol=position.symbol, direction=position.direction, opened_at=position.opened_at, closed_at=position.closed_at,
            quantity=position.quantity, entry_price=position.entry_price, exit_price=position.exit_price,
            realized_pnl=position.realized_pnl, commission=position.commission, net=position.net,
            source="program" if plan else "manual", analysis_id=plan.id if plan else None, close_reason=plan.close_reason if plan else None,
        ))
    items = [
        i for i in items
        if (symbol is None or i.symbol == symbol) and (start is None or i.closed_at >= start) and (end is None or i.closed_at < end)
        and (outcome is None or (i.net > 0 if outcome == "profit" else i.net < 0)) and (source is None or i.source == source)
    ]
    items.sort(key=lambda i: i.closed_at, reverse=True)
    summary = PositionSummary(
        count=len(items), wins=sum(1 for i in items if i.net > 0),
        net=sum((i.net for i in items), Decimal(0)), commission=sum((i.commission for i in items), Decimal(0)),
    )
    return PositionHistoryPage(summary=summary, total=len(items), items=items[(page - 1) * limit:page * limit])


@router.get("/positions/history/symbols")
async def list_position_history_symbols(session: Annotated[AsyncSession, Depends(get_session)]) -> list[str]:
    """有过成交的币，供历史仓位筛选"""
    return list(await session.scalars(select(TradingFill.symbol).distinct().order_by(TradingFill.symbol)))


@router.post("/symbols/{symbol}/analyze", status_code=status.HTTP_202_ACCEPTED)
async def request_analysis(symbol: str, session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    """排队一次手动分析：该币停止交易时为试运行，计划只记录、不下单"""
    s = await get_symbol(session, symbol)
    if s.capital_usdt is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{symbol} 还没有设置交易资金")
    running = await session.scalar(select(func.count()).where(TradingAnalysis.symbol == symbol, TradingAnalysis.status == "running"))
    if running or s.analysis_requested_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{symbol} 已在分析或排队中")
    s.analysis_requested_at = datetime.now(LOCAL_TZ)
    s.analysis_reasons = ["手动分析"]
    await session.commit()


@router.get("/analyses")
async def list_analyses(
    session: Annotated[AsyncSession, Depends(get_session)],
    symbol: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[AnalysisOut]:
    query = select(TradingAnalysis).order_by(TradingAnalysis.id.desc()).limit(limit)
    if symbol:
        query = query.where(TradingAnalysis.symbol == symbol)
    return [AnalysisOut.model_validate(a, from_attributes=True) for a in await session.scalars(query)]


@router.get("/analyses/{analysis_id}")
async def get_analysis(analysis_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> AnalysisDetail:
    analysis = await session.get(TradingAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分析不存在")
    return AnalysisDetail.model_validate(analysis, from_attributes=True)


@router.get("/symbols/{symbol}/plan")
async def get_symbol_plan(symbol: str, session: Annotated[AsyncSession, Depends(get_session)]) -> SymbolPlanOut:
    plan = await session.scalar(
        select(TradingAnalysis).where(TradingAnalysis.symbol == symbol, TradingAnalysis.plan_status.in_(("proposed", *LIVE_PLAN_STATUSES)))
        .order_by(TradingAnalysis.id.desc()).limit(1)
    )
    if plan is None:
        return SymbolPlanOut(plan=None, orders=[])
    orders = await session.scalars(select(TradingOrder).where(TradingOrder.analysis_id == plan.id).order_by(TradingOrder.id))
    return SymbolPlanOut(
        plan=AnalysisOut.model_validate(plan, from_attributes=True),
        orders=[OrderOut.model_validate(o, from_attributes=True) for o in orders],
    )
