"""模拟盘交易进程：同步账户快照、执行器对账、调度分析、记录新闻信号。

用法：python -m alphapulse.trading.engine
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update

from alphapulse.core.db import SessionLocal
from alphapulse.models import TradingAnalysis, TradingSymbol
from alphapulse.trading.analyst import run_analysis
from alphapulse.trading.executor import executor_loop
from alphapulse.trading.news_signals import news_signal_loop
from alphapulse.trading.schedule import routine_requests
from alphapulse.trading.snapshot import SnapshotError, read_snapshot, require_fresh_snapshot, snapshot_loop

logger = logging.getLogger("alphapulse.trading.engine")

ANALYSIS_POLL_SECONDS = 5
# 模型调用耗时长，并发过多会撞上游限流
MAX_CONCURRENT_ANALYSES = 2


async def fail_interrupted() -> None:
    """上次进程退出时还在进行的分析不会再有结果"""
    async with SessionLocal() as session:
        await session.execute(
            update(TradingAnalysis).where(TradingAnalysis.status == "running")
            .values(status="failed", error="引擎重启，分析中断", finished_at=datetime.now(UTC))
        )
        await session.commit()


async def claim_requests(limit: int, busy: set[str]) -> list[tuple[str, list[str]]]:
    """取出到期的分析请求并清空，同一请求里的多个触发原因合并为一次分析"""
    try:
        require_fresh_snapshot(await read_snapshot())
    except SnapshotError:
        # 领取后就会创建分析记录；账户未就绪时保留请求，避免空耗次数和分析间隔。
        return []
    async with SessionLocal() as session:
        symbols = (await session.scalars(
            select(TradingSymbol)
            .where(TradingSymbol.analysis_requested_at <= datetime.now(UTC), TradingSymbol.symbol.not_in(busy))
            .order_by(TradingSymbol.analysis_requested_at).limit(limit)
        )).all()
        claimed = [(s.symbol, list(s.analysis_reasons)) for s in symbols]
        for s in symbols:
            s.analysis_requested_at = None
            s.analysis_reasons = []
        await session.commit()
    return claimed


async def analysis_loop() -> None:
    await fail_interrupted()
    tasks: dict[str, asyncio.Task] = {}
    while True:
        try:
            for symbol, task in list(tasks.items()):
                if task.done():
                    del tasks[symbol]
                    if not task.cancelled() and task.exception():
                        logger.error("%s 分析任务异常退出", symbol, exc_info=task.exception())
            await routine_requests()
            free = MAX_CONCURRENT_ANALYSES - len(tasks)
            if free > 0:
                for symbol, reasons in await claim_requests(free, set(tasks)):
                    tasks[symbol] = asyncio.create_task(run_analysis(symbol, reasons))
        except Exception:
            logger.exception("调度分析出错，稍后重试")
        await asyncio.sleep(ANALYSIS_POLL_SECONDS)


async def main() -> None:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(snapshot_loop())
        tg.create_task(executor_loop())
        tg.create_task(analysis_loop())
        tg.create_task(news_signal_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
