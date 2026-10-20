"""自动分析的排队：触发式（成交、止盈、平仓、到期、条件满足）和例行。只在交易中排队，手动分析走 API"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphapulse.ai.tools import LOCAL_TZ
from alphapulse.core.db import SessionLocal
from alphapulse.models import TradingAccount, TradingAnalysis, TradingEvent, TradingSymbol
from alphapulse.trading.client import active_exchange
from alphapulse.trading.plan import MIN_ANALYSIS_GAP, next_routine_at

# 自动分析失败后的重试：间隔、连续失败上限
RETRY_AFTER = timedelta(minutes=5)
MAX_CONSECUTIVE_FAILURES = 3


async def daily_limit_reached(session: AsyncSession) -> bool:
    account = await session.get(TradingAccount, 1)
    limit = account.daily_analysis_limit if account else 60
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    count = await session.scalar(select(func.count()).where(TradingAnalysis.started_at >= today))
    if count < limit:
        return False
    noticed = await session.scalar(select(func.count()).where(TradingEvent.kind == "daily_limit", TradingEvent.created_at >= today))
    if not noticed:
        session.add(TradingEvent(exchange=await active_exchange(session), kind="daily_limit", level="warn",
                                 message=f"今日分析已达 {limit} 次上限，暂停自动分析，继续执行程序规则（止损、止盈、到期平仓等）"))
    return True


async def request_analysis(session: AsyncSession, symbol: str, reason: str) -> None:
    """排队一次自动分析：多个原因合并；距上次分析不足最短间隔时顺延。调用方负责 commit"""
    target = await session.get(TradingSymbol, symbol)
    if target is None or not target.running or await daily_limit_reached(session):
        return
    now = datetime.now(UTC)
    last_started = await session.scalar(select(func.max(TradingAnalysis.started_at)).where(TradingAnalysis.exchange == await active_exchange(session), TradingAnalysis.symbol == symbol))
    at = max(now, last_started + MIN_ANALYSIS_GAP) if last_started else now
    target.analysis_requested_at = min(target.analysis_requested_at, at) if target.analysis_requested_at else at
    if reason not in target.analysis_reasons:
        # JSONB 列要整体赋值才会被识别为修改
        target.analysis_reasons = [*target.analysis_reasons, reason]


async def routine_requests() -> None:
    """交易中的币：到例行时间的排队例行分析；自动分析失败的隔一段时间重试"""
    async with SessionLocal() as session:
        now = datetime.now(UTC)
        for target in (await session.scalars(select(TradingSymbol).where(TradingSymbol.running))).all():
            if target.analysis_requested_at is not None:
                continue
            recent = (await session.scalars(
                select(TradingAnalysis).where(TradingAnalysis.exchange == await active_exchange(session), TradingAnalysis.symbol == target.symbol)
                .order_by(TradingAnalysis.id.desc()).limit(MAX_CONSECUTIVE_FAILURES)
            )).all()
            if recent and recent[0].status == "running":
                continue
            if not recent or now >= next_routine_at(recent[0].started_at):
                await request_analysis(session, target.symbol, "例行分析")
                continue
            last = recent[0]
            all_failed = len(recent) == MAX_CONSECUTIVE_FAILURES and all(a.status == "failed" for a in recent)
            if last.status == "failed" and last.reasons != ["手动分析"] and not all_failed and now >= last.finished_at + RETRY_AFTER:
                await request_analysis(session, target.symbol, f"重试：{'；'.join(last.reasons)}")
        await session.commit()
