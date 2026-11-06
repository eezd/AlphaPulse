"""One bounded pool for indexing, assignment and event research; no third stage."""

import asyncio
import logging
from sqlalchemy import select, text, update
from alphapulse.core.db import SessionLocal, engine
from alphapulse.news_research.models import (
    ResearchSettings,
    Package,
    Event,
    Attempt,
    Version,
    Batch, BatchEvent,
)
from alphapulse.news_research.service import process_package, process_event
from alphapulse.news_research import batches, scheduling

logger = logging.getLogger(__name__)


async def recover_interrupted():
    async with SessionLocal() as session:
        for stage in ("index", "assign"):
            await session.execute(
                update(Package)
                .where(Package.state == stage + "_processing")
                .values(state=stage + "_failed", error="进程中断，调用结果不明，请手动重试")
            )
        await session.execute(
            update(Attempt)
            .where(Attempt.state == "processing")
            .values(state="failed", error="进程中断，调用结果不明，请手动重试")
        )
        await session.execute(
            update(Event).where(Event.state == "processing").values(state="failed", error="进程中断，调用结果不明，请手动重试")
        )
        await session.execute(
            update(Version)
            .where(Version.state == "processing")
            .values(state="failed", error="进程中断，调用结果不明，请手动重试")
        )
        await session.execute(update(BatchEvent).where(BatchEvent.state == 'processing').values(state='pending'))
        await session.commit()


async def run():
    async with engine.connect() as lock:
        if not await lock.scalar(text("SELECT pg_try_advisory_lock(20261030, 1)")):
            raise RuntimeError("新闻AI worker 已运行")
        await lock.commit()
        await recover_interrupted()
        tasks = {}
        try:
            while True:
                for task in list(tasks):
                    if task.done():
                        tasks.pop(task)
                        try:
                            task.result()
                        except Exception:
                            logger.exception("新闻研究任务失败")
                async with SessionLocal() as session:
                    settings = await session.get(ResearchSettings, 1)
                    if settings and settings.enabled:
                        active = set(tasks.values())
                        slots = max(0, settings.config.get('concurrency', 3) - len(tasks))
                        actions = await scheduling.realtime_actions(session, settings, active, slots)
                        batch = await session.scalar(select(Batch).where(
                            Batch.phase.in_(['assigning', 'researching'])).order_by(Batch.id).limit(1))
                        if batch is None:
                            batch = await batches.freeze(session, settings)
                        if batch is not None:
                            actions += await batches.actions(session, batch, active | set(actions), slots - len(actions), settings)
                        await session.commit()
                        config = {**settings.config, '_control_revision': settings.control_revision}
                        for stage, identifier in actions:
                            call = (process_event(identifier, config)
                                    if stage == 'event' else process_package(identifier, config))
                            tasks[asyncio.create_task(call)] = (stage, identifier)
                await asyncio.sleep(1)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
