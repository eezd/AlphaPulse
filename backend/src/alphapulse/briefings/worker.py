"""单一有界池：到期实时报、实时新闻、历史新闻、历史报表。"""
import asyncio
import logging
import signal
from datetime import UTC, datetime
from sqlalchemy import select, text
from alphapulse.core.db import SessionLocal, engine
from alphapulse.briefings import config, notifications, reports, service
from alphapulse.briefings.models import Analysis, Report
from alphapulse.ai import progress

logger = logging.getLogger(__name__)


async def candidates(session, configuration):
    result = []
    for lane in ('live', 'historical_backfill'):
        # 实时报告优先、历史分析先于历史报表。
        kinds = ('report', 'analysis') if lane == 'live' else ('analysis', 'report')
        for kind in kinds:
            cls = Report if kind == 'report' else Analysis
            query = select(cls.id).where(cls.state == 'pending', cls.lane == lane)
            if kind == 'report':
                query = query.where(service.report_scope(configuration), Report.input.is_not(None), Report.next_check_at <= datetime.now(UTC)).order_by(Report.due_at, Report.id)
            else:
                query = query.where(service.analysis_scope(configuration)).order_by(Analysis.effective_at, Analysis.id)
            result += [(kind, identifier, lane) for identifier in await session.scalars(query.limit(32))]
    return result


async def claimed_actions(session):
    claimed = set()
    for kind, cls in (('analysis', Analysis), ('report', Report)):
        rows = await session.execute(select(cls.id, cls.control_revision).where(cls.state == 'processing'))
        claimed.update((kind, identifier, revision) for identifier, revision in rows)
    return claimed


async def run():
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stopping.set))
    async with engine.connect() as lock:
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(20261030, 1)')):
            raise RuntimeError('新闻AI worker 已运行')
        await lock.commit()
        async with SessionLocal() as session:
            await config.settings_row(session, lock=True)
            await service.recover(session)
            await notifications.recover(session)
            await session.commit()
        tasks, retiring = {}, set()
        revision = None
        reporter = asyncio.create_task(progress.report())
        sender = asyncio.create_task(notifications.sender(stopping))
        try:
            while not stopping.is_set():
                for task in list(tasks) + list(retiring):
                    if task.done():
                        tasks.pop(task, None)
                        retiring.discard(task)
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            logger.exception('新闻/报表任务失败')
                if sender.done():
                    sender.result()
                    raise RuntimeError('异常通知发送器意外停止')
                async with SessionLocal() as session:
                    settings = await config.settings_row(session, lock=True)
                    if tasks and revision != settings.control_revision:
                        claimed = await claimed_actions(session)
                        for task, (kind, identifier, task_revision, _) in list(tasks.items()):
                            if (kind, identifier, task_revision) not in claimed:
                                task.cancel()
                                retiring.add(task)
                                tasks.pop(task)
                    revision = settings.control_revision
                    actions = []
                    if settings.enabled:
                        now = datetime.now(UTC)
                        await service.enroll(session, settings, now)
                        failing = progress.upstream_failures()
                        for configuration in failing:
                            await notifications.enqueue_upstream_alert(session, configuration, settings)
                        await reports.prepare_due(session, settings, now, upstream=failing)
                        choices = await candidates(session, settings.config)
                        active = {(kind, identifier) for kind, identifier, _, _ in tasks.values()}
                        live_ready = any(lane == 'live' and (kind, identifier) not in active for kind, identifier, lane in choices)
                        concurrency = settings.config.get('concurrency', 3)
                        history_running = sum(item[3] == 'historical_backfill' for item in tasks.values())
                        for kind, identifier, lane in choices:
                            if len(tasks) + len(actions) >= concurrency:
                                break
                            if (kind, identifier) in active:
                                continue
                            if lane != 'live' and live_ready and history_running >= max(0, concurrency - 1):
                                continue
                            actions.append((kind, identifier, revision, lane))
                            if lane != 'live':
                                history_running += 1
                    await session.commit()
                    if stopping.is_set():
                        break
                    for kind, identifier, task_revision, lane in actions:
                        call = (reports.process_report(identifier, task_revision) if kind == 'report'
                                else service.process_analysis(identifier, task_revision))
                        tasks[asyncio.create_task(progress.execute((kind, identifier), call))] = (kind, identifier, task_revision, lane)
                if tasks:
                    await asyncio.wait(tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(1)
        finally:
            stopping.set()
            try:
                await asyncio.gather(*tasks, *retiring, return_exceptions=True)
                await sender
            finally:
                reporter.cancel()
                sender.cancel()
                await asyncio.gather(reporter, sender, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(run())
