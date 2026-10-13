"""采集管理进程：按间隔触发采集、守护每个来源的 scrapy 子进程、处理重启指令。

用法（工作目录需包含 scrapy.cfg）：python -m alphapulse.crawler.manager
"""

import asyncio
import json
import logging
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from alphapulse.core.db import SessionLocal
from alphapulse.core.redis import KEY_PREFIX, redis
from alphapulse.crawler.events import CONTROL_CHANNEL, STATUS_KEY, start_urls_key, write_logs
from alphapulse.models import CrawlRun, Source

logger = logging.getLogger("alphapulse.crawler.manager")

RESTART_DELAY_MIN = 5
RESTART_DELAY_MAX = 300
# 子进程稳定运行超过该时长后，异常退出的重启延迟重新从最小值开始
STABLE_SECONDS = 60
STOP_TIMEOUT = 15


@dataclass
class Worker:
    code: str
    proc: asyncio.subprocess.Process | None = None
    started_at: datetime | None = None
    next_run_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 下一次触发的类型：schedule / startup / restart
    trigger: str = "schedule"
    restarting: bool = False
    restart_delay: int = RESTART_DELAY_MIN
    wake: asyncio.Event = field(default_factory=asyncio.Event)


async def log(code: str, level: str, message: str) -> None:
    logger.log(logging.getLevelName(level), "[%s] %s", code, message)
    await write_logs([(code, None, level, message)])


class Manager:
    def __init__(self) -> None:
        self.workers: dict[str, Worker] = {}
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        async with SessionLocal() as session:
            codes = (await session.scalars(select(Source.code).order_by(Source.code))).all()
        self.workers = {code: Worker(code) for code in codes}

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)

        tasks = [asyncio.create_task(self.supervise(w)) for w in self.workers.values()]
        tasks += [asyncio.create_task(c) for c in (self.schedule_loop(), self.control_loop(), self.status_loop())]
        await self.stopping.wait()
        logger.info("收到退出信号，停止全部子进程")
        await asyncio.gather(*(self.stop_process(w) for w in self.workers.values()))
        for task in tasks:
            task.cancel()
        await redis.delete(STATUS_KEY)

    async def supervise(self, w: Worker) -> None:
        while not self.stopping.is_set():
            # 上次进程遗留的请求和未收尾的轮次都已失效，清理后从干净状态开始
            await redis.delete(f"{KEY_PREFIX}{w.code}:requests", start_urls_key(w.code))
            async with SessionLocal() as session:
                await session.execute(
                    update(CrawlRun)
                    .where(CrawlRun.source_code == w.code, CrawlRun.status == "running")
                    .values(status="interrupted", finished_at=datetime.now(UTC), error="子进程退出时本轮未完成")
                )
                await session.commit()
            w.proc = await asyncio.create_subprocess_exec("scrapy", "crawl", w.code)
            w.started_at = datetime.now(UTC)
            # 新进程的第一轮立即执行，并做全量回溯扫描
            if w.trigger == "schedule":
                w.trigger = "startup"
            w.next_run_at = w.started_at
            await log(w.code, "INFO", f"子进程已启动 pid={w.proc.pid}")
            returncode = await w.proc.wait()
            ran_seconds = (datetime.now(UTC) - w.started_at).total_seconds()
            w.proc = None
            if self.stopping.is_set():
                return
            if w.restarting:
                w.restarting = False
                w.restart_delay = RESTART_DELAY_MIN
                continue
            if ran_seconds >= STABLE_SECONDS:
                w.restart_delay = RESTART_DELAY_MIN
            await log(w.code, "ERROR", f"子进程异常退出 returncode={returncode}，{w.restart_delay}s 后重启")
            w.wake.clear()
            try:
                await asyncio.wait_for(w.wake.wait(), timeout=w.restart_delay)
            except TimeoutError:
                pass
            w.restart_delay = min(w.restart_delay * 2, RESTART_DELAY_MAX)

    async def stop_process(self, w: Worker) -> None:
        if w.proc is None:
            return
        w.proc.terminate()
        try:
            await asyncio.wait_for(w.proc.wait(), timeout=STOP_TIMEOUT)
        except TimeoutError:
            await log(w.code, "WARNING", f"子进程 {STOP_TIMEOUT}s 内未退出，强制结束")
            w.proc.kill()

    async def schedule_loop(self) -> None:
        while True:
            async with SessionLocal() as session:
                sources = (await session.scalars(select(Source))).all()
            now = datetime.now(UTC)
            for s in sources:
                w = self.workers[s.code]
                if not s.enabled or w.proc is None or w.restarting:
                    continue
                if s.paused_until and s.paused_until > now:
                    w.next_run_at = max(w.next_run_at, s.paused_until)
                    continue
                if now < w.next_run_at:
                    continue
                # 子进程还没取走上一条触发时不再堆积
                if await redis.llen(start_urls_key(s.code)) == 0:
                    await redis.lpush(start_urls_key(s.code), json.dumps({"trigger": w.trigger, "backfill_hours": s.backfill_hours}))
                    w.trigger = "schedule"
                w.next_run_at = now + timedelta(seconds=s.interval_seconds)
            await asyncio.sleep(1)

    async def control_loop(self) -> None:
        async with redis.pubsub() as pubsub:
            await pubsub.subscribe(CONTROL_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                command = json.loads(message["data"])
                if command["action"] != "restart":
                    logger.warning("未知指令：%s", command)
                    continue
                codes = list(self.workers) if command["source"] == "all" else [command["source"]]
                await asyncio.gather(*(self.restart(self.workers[code]) for code in codes))

    async def restart(self, w: Worker) -> None:
        await log(w.code, "INFO", "收到重启指令")
        async with SessionLocal() as session:
            await session.execute(
                update(Source).where(Source.code == w.code).values(consecutive_failures=0, paused_until=None)
            )
            await session.commit()
        w.trigger = "restart"
        w.next_run_at = datetime.now(UTC)
        if w.proc is None:
            # 处于异常退出后的等待期：立即重启
            w.wake.set()
            return
        w.restarting = True
        await self.stop_process(w)

    async def status_loop(self) -> None:
        while True:
            mapping = {
                w.code: json.dumps({
                    "state": "restarting" if w.restarting else ("running" if w.proc else "stopped"),
                    "pid": w.proc.pid if w.proc else None,
                    "started_at": w.started_at.isoformat() if w.started_at else None,
                    "next_run_at": w.next_run_at.isoformat(),
                })
                for w in self.workers.values()
            }
            async with redis.pipeline() as pipe:
                # 管理进程挂掉后状态自动过期，前端据此显示"管理进程未运行"
                await pipe.hset(STATUS_KEY, mapping=mapping).expire(STATUS_KEY, 10).execute()
            await asyncio.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Manager().run())
