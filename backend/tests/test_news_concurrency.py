"""Concurrency behavior uses controlled tasks and isolated I/O boundaries."""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock
import pytest


def fake_session(settings):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    session.get = AsyncMock(return_value=settings)
    session.scalars = AsyncMock(return_value=N(all=lambda: []))
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return session


def test_dispatch_refills_without_waiting_for_slow_tasks_and_respects_live_limit(trading, monkeypatch):
    from alphapulse.news_risk import scheduler
    async def check():
        settings = N(enabled=True, max_concurrency=5)
        session = fake_session(settings)
        monkeypatch.setattr(scheduler, 'SessionLocal', lambda: session)
        monkeypatch.setattr(scheduler.redis, 'set', AsyncMock())
        lock = N(scalar=AsyncMock(return_value=True), execute=AsyncMock(), commit=AsyncMock())
        releases = [asyncio.Event() for _ in range(10)]
        started = []
        async def task(index):
            started.append(index)
            await releases[index].wait()
        async def pick(active, cursor, allow):
            assert len(active) < settings.max_concurrency
            index = len(started)
            # Reserve before the spawned coroutine starts.
            index = max([key[1] for key in active] + [index-1]) + 1
            if index >= len(releases): return None
            return (('grouping', 'analysis', 'events', 'outlook')[index % 4], index), lambda: task(index), cursor
        monkeypatch.setattr(scheduler, 'candidate', pick)
        run = asyncio.create_task(scheduler.run(lock))
        async def until(predicate):
            async with asyncio.timeout(3):
                while not predicate(): await asyncio.sleep(.001)
        try:
            await until(lambda: len(started) == 5)
            releases[0].set(); releases[1].set()
            await until(lambda: len(started) == 7)
            assert all(not releases[i].is_set() for i in (2,3,4))
            settings.max_concurrency = 2
            for i in (2,3,4,5): releases[i].set()
            await until(lambda: len(started) == 8)
            assert not releases[6].is_set()
            assert len(started) == 8
            settings.enabled = False
            releases[6].set(); releases[7].set()
            await asyncio.sleep(.03)
            assert len(started) == 8
            settings.enabled = True; settings.max_concurrency = 5
            await until(lambda: len(started) == 10)
        finally:
            run.cancel()
            with pytest.raises(asyncio.CancelledError): await run
        assert lock.execute.await_count >= 1
    asyncio.run(check())


def test_failed_tasks_release_capacity(trading):
    from alphapulse.news_risk.scheduler import Pool
    async def check():
        pool = Pool()
        async def fail(): raise RuntimeError('Synthetic failure')
        pool.start(('analysis', 1), fail)
        await pool.reap()
        assert not pool.tasks
        await pool.close()
    asyncio.run(check())


@pytest.mark.parametrize('value', [0, -1, 1.5, '5', True, None])
def test_concurrency_requires_a_positive_integer(trading, value):
    from pydantic import ValidationError
    from alphapulse.api.risk import ConcurrencyIn
    with pytest.raises(ValidationError): ConcurrencyIn(max_concurrency=value)


def test_operational_update_does_not_pause_or_change_model_settings(trading, monkeypatch):
    from alphapulse.api import risk
    settings = N(enabled=True, max_concurrency=5, model='original')
    monkeypatch.setattr(risk, 'default_settings', AsyncMock(return_value=settings))
    asyncio.run(risk.save_concurrency(risk.ConcurrencyIn(max_concurrency=3), N(commit=AsyncMock(), add=MagicMock())))
    assert settings.enabled and settings.model == 'original' and settings.max_concurrency == 3


@pytest.mark.parametrize('busy,enabled', [(True,False), (False,True)])
def test_validation_never_bypasses_background_tasks(trading, monkeypatch, busy, enabled):
    from fastapi import HTTPException
    from alphapulse.api import risk
    from alphapulse.core import db
    gate = MagicMock()
    gate.__aenter__ = AsyncMock(return_value=gate); gate.__aexit__ = AsyncMock(return_value=None)
    gate.scalar = AsyncMock(return_value=not busy)
    gate.execute = AsyncMock(); gate.commit = AsyncMock()
    monkeypatch.setattr(db, 'engine', N(connect=lambda: gate))
    validate = AsyncMock()
    monkeypatch.setattr(risk, 'validate_enable', validate)
    session = N(get=AsyncMock(return_value=N(enabled=enabled)))
    with pytest.raises(HTTPException) as error:
        asyncio.run(risk.enable(risk.SettingsIn(),session))
    assert error.value.status_code == 409
    validate.assert_not_awaited()


def test_history_completion_is_counted_independently_of_item_order(trading, monkeypatch):
    from alphapulse.news_risk import reprocess, worker
    from alphapulse.models.risk import RiskSettings, ArticleAnalysis, ReprocessJob
    from alphapulse.models import NewsItem
    job = N(id=4, state='running', position=0, settings={}, failures=[],
        items=[{'id':i,'hash':'current','state':'pending'} for i in (1,2,3)])
    article = N(id=30, state='done', error=None)
    session = fake_session(None)
    session.scalar = AsyncMock(side_effect=[job, None, article])
    session.get = AsyncMock(side_effect=lambda model,*a,**kw: {
        RiskSettings:N(enabled=True), ReprocessJob:job,
        ArticleAnalysis:article, NewsItem:N(id=3, content_hash='current')}[model])
    monkeypatch.setattr(reprocess, 'SessionLocal', lambda:session)
    monkeypatch.setattr(reprocess, 'frozen_configuration', AsyncMock())
    monkeypatch.setattr(worker, 'analyze', AsyncMock(return_value='done'))
    asyncio.run(reprocess.process_job(2))
    assert job.position == 1
    assert [i['state'] for i in job.items] == ['pending','pending','done']
    assert job.failures == []
