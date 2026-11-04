"""One bounded pool for indexing, assignment and event research; no third stage."""

import asyncio
import logging
from datetime import UTC, datetime
from sqlalchemy import select, text, update, exists
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.news_research.models import (
    ResearchSettings,
    Package,
    Event,
    Attempt,
    Version,
    Membership,
)
from alphapulse.news_research.service import enroll, process_package, process_event

logger = logging.getLogger(__name__)


async def run():
    async with engine.connect() as lock:
        if not await lock.scalar(text("SELECT pg_try_advisory_lock(20261030, 1)")):
            raise RuntimeError("新闻AI worker 已运行")
        await lock.commit()
        async with SessionLocal() as session:
            for stage in ("index", "assign"):
                await session.execute(
                    update(Package)
                    .where(Package.state == stage + "_processing")
                    .values(state=stage + "_pending")
                )
            await session.execute(
                update(Attempt)
                .where(Attempt.state == "processing")
                .values(state="failed", error="进程中断，待重试")
            )
            await session.execute(
                update(Event).where(Event.state == "processing").values(state="pending")
            )
            await session.execute(
                update(Version)
                .where(Version.state == "processing")
                .values(state="failed", error="进程中断，待重试")
            )
            await session.commit()
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
                        ids = list(
                            await session.scalars(
                                select(NewsItem.id)
                                .outerjoin(Package, Package.news_id == NewsItem.id)
                                .where(
                                    NewsItem.published_at >= settings.start_at,
                                    NewsItem.published_at <= datetime.now(UTC),
                                    (Package.id.is_(None))
                                    | (Package.content_hash != NewsItem.content_hash),
                                )
                                .order_by(NewsItem.published_at.desc())
                                .limit(100)
                            )
                        )
                        await enroll(session, ids)
                        active = set(tasks.values())
                        assigning = any(key[0] == "assign" for key in active)
                        slots = settings.config.get("concurrency", 3) - len(tasks)
                        packages = list(
                            await session.scalars(
                                select(Package)
                                .where(Package.state == "index_pending")
                                .order_by(Package.id.desc())
                                .limit(max(0, slots))
                            )
                        )
                        if not assigning and slots > 0:
                            assignment = await session.scalar(
                                select(Package)
                                .where(Package.state == "assign_pending")
                                .order_by(Package.id.desc())
                                .limit(1)
                            )
                            if assignment is not None:
                                packages.insert(0, assignment)
                        events = list(
                            await session.scalars(
                                select(Event)
                                .where(
                                    Event.state == "pending",
                                    Event.due_at <= datetime.now(UTC),
                                    ~exists(
                                        select(Membership.event_id)
                                        .join(
                                            Package, Package.id == Membership.package_id
                                        )
                                        .join(NewsItem, NewsItem.id == Package.news_id)
                                        .where(
                                            Membership.event_id == Event.id,
                                            (Package.state != "done")
                                            | (
                                                Package.content_hash
                                                != NewsItem.content_hash
                                            ),
                                        )
                                    ),
                                )
                                .order_by(Event.due_at)
                                .limit(max(0, slots))
                            )
                        )
                        # Interleave ready event work with new indexing to avoid starvation.
                        actions = []
                        for i in range(max(len(packages), len(events))):
                            if i < len(events):
                                actions.append((("event", events[i].id), process_event))
                            if i < len(packages):
                                actions.append(
                                    (
                                        (
                                            "assign"
                                            if packages[i].state == "assign_pending"
                                            else "index",
                                            packages[i].id,
                                        ),
                                        process_package,
                                    )
                                )
                        for key, action in actions:
                            if len(tasks) >= settings.config.get("concurrency", 3):
                                break
                            if key not in active:
                                tasks[
                                    asyncio.create_task(action(key[1], settings.config))
                                ] = key
                await asyncio.sleep(1)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
