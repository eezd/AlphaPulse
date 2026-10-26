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
CONTROL_RECONNECT_SECONDS = 5


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
    """写 stdout 和 crawl_logs；写库失败只记 stdout，不能让守护任务因为日志而中断"""
    logger.log(logging.getLevelName(level), "[%s] %s", code, message)
    try:
        await write_logs([(code, None, level, message)])
    except Exception:
        logger.exception("[%s] 日志落库失败", code)


class Manager:
    def __init__(self) -> None:
        self.workers: dict[str, Worker] = {}
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        async with SessionLocal() as session:
            codes = (await session.scalars(select(Source.code).where(Source.collector == "news").order_by(Source.code))).all()
        self.workers = {code: Worker(code) for code in codes}

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)

        # 各循环自行重试可恢复的错误；仍有异常逃出时 TaskGroup 让整个进程退出，由 docker 重启，
        # 避免出现"进程活着但调度或守护任务已经死掉"的状态
        async with asyncio.TaskGroup() as tg:
            for w in self.workers.values():
                tg.create_task(self.supervise(w))
            loops = [tg.create_task(c) for c in (self.schedule_loop(), self.control_loop(), self.status_loop())]
            await self.stopping.wait()
            logger.info("收到退出信号，停止全部子进程")
            await asyncio.gather(*(self.stop_process(w) for w in self.workers.values()))
            for task in loops:
                task.cancel()
        await redis.delete(STATUS_KEY)

    async def supervise(self, w: Worker) -> None:
        while not self.stopping.is_set():
            await self.cleanup_before_spawn(w)
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

    async def cleanup_before_spawn(self, w: Worker) -> None:
        """上次进程遗留的请求和未收尾的轮次都已失效；清理失败不影响拉起子进程"""
        try:
            await redis.delete(f"{KEY_PREFIX}{w.code}:requests", start_urls_key(w.code))
            async with SessionLocal() as session:
                await session.execute(
                    update(CrawlRun)
                    .where(CrawlRun.source_code == w.code, CrawlRun.status == "running", CrawlRun.trigger != "backfill")
                    .values(status="interrupted", finished_at=datetime.now(UTC), error="子进程退出时本轮未完成")
                )
                await session.commit()
        except Exception:
            logger.exception("[%s] 拉起前清理失败，继续启动子进程", w.code)

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
        unknown: set[str] = set()
        while True:
            try:
                await self.schedule_once(unknown)
            except Exception:
                logger.exception("调度出错，1s 后重试")
            await asyncio.sleep(1)

    async def schedule_once(self, unknown: set[str]) -> None:
        async with SessionLocal() as session:
            sources = (await session.scalars(select(Source).where(Source.collector == "news"))).all()
        now = datetime.now(UTC)
        for s in sources:
            w = self.workers.get(s.code)
            if w is None:
                # 启动后才新增的来源，需要重启管理进程才会接管
                if s.code not in unknown:
                    unknown.add(s.code)
                    logger.warning("来源 %s 不在本进程的守护列表中，重启管理进程后生效", s.code)
                continue
            if not s.enabled or w.proc is None or w.restarting:
                continue
            if s.paused_until and s.paused_until > now:
                w.next_run_at = max(w.next_run_at, s.paused_until)
                continue
            if now < w.next_run_at:
                continue
            # 子进程还没取走上一条触发时不再堆积
            if await redis.llen(start_urls_key(s.code)) == 0:
                tick = {"trigger": w.trigger, "backfill_hours": s.backfill_hours}
                if s.history_target and not s.history_done:
                    # 回补分段夹在常规轮次里执行，进度由子进程在本轮成功后写回
                    position = s.history_position.isoformat() if s.history_position else None
                    tick["history"] = {"target": s.history_target.isoformat(), "position": position}
                await redis.lpush(start_urls_key(s.code), json.dumps(tick))
                w.trigger = "schedule"
            w.next_run_at = now + timedelta(seconds=s.interval_seconds)

    async def control_loop(self) -> None:
        while True:
            try:
                async with redis.pubsub() as pubsub:
                    await pubsub.subscribe(CONTROL_CHANNEL)
                    async for message in pubsub.listen():
                        if message["type"] == "message":
                            await self.handle_command(message["data"])
            except Exception:
                logger.exception("控制通道断开，%ds 后重新订阅", CONTROL_RECONNECT_SECONDS)
                await asyncio.sleep(CONTROL_RECONNECT_SECONDS)

    async def handle_command(self, raw: str) -> None:
        """单条指令出错只记日志，不影响后续指令"""
        try:
            command = json.loads(raw)
            if command.get("action") != "restart":
                logger.warning("未知指令：%s", raw)
                return
            source = command.get("source")
            codes = list(self.workers) if source == "all" else [source]
            if any(code not in self.workers for code in codes):
                logger.warning("重启指令中的来源不存在：%s", raw)
                return
            await asyncio.gather(*(self.restart(self.workers[code]) for code in codes))
        except Exception:
            logger.exception("处理指令失败：%s", raw)

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
            try:
                await self.publish_status()
            except Exception:
                logger.exception("发布状态失败，2s 后重试")
            await asyncio.sleep(2)

    async def publish_status(self) -> None:
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Manager().run())
