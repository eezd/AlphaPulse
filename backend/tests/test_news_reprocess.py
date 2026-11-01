"""Reprocessing checkpoints and retries isolate work by the failed stage."""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock
import pytest


def session_mock():
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    for name in ('get', 'scalar', 'scalars', 'commit', 'rollback', 'flush', 'execute'):
        setattr(session, name, AsyncMock())
    return session


def test_pause_after_claim_does_not_count_old_success(trading, monkeypatch):
    from alphapulse.news_risk import reprocess, worker
    from alphapulse.models import NewsItem
    from alphapulse.models.risk import RiskSettings, ReprocessJob, ArticleAnalysis
    job = N(id=20, state='pending', position=0, items=[{'id':21,'hash':'abc'}], failures=[], settings={})
    old = N(id=25, state='done', error=None, result={'old_success':True})
    session = session_mock()
    session.scalar.side_effect = [None, None, job, old]
    session.get.side_effect = lambda model, *a, **kw: {
        RiskSettings:N(enabled=True), NewsItem:N(id=21,content_hash='abc'), ArticleAnalysis:old, ReprocessJob:job}[model]
    paused = session_mock()
    paused.get.return_value = N(enabled=False)
    model = AsyncMock(side_effect=AssertionError('Paused work must not call a model'))
    monkeypatch.setattr(reprocess, 'SessionLocal', lambda:session)
    monkeypatch.setattr(reprocess, 'frozen_configuration', AsyncMock(return_value=(N(),'rules',{})))
    monkeypatch.setattr(worker, 'SessionLocal', lambda:paused)
    monkeypatch.setattr(worker, 'complete', model)
    asyncio.run(reprocess.process_job())
    assert job.position == 0 and job.failures == []
    assert old.result == {'old_success':True}
    model.assert_not_awaited()


@pytest.mark.parametrize('stage', ['analysis', 'events', 'outlook'])
def test_downstream_failure_does_not_mark_unrelated_news_failed(trading, monkeypatch, stage):
    from alphapulse.news_risk import reprocess, events
    job = N(id=10,state='running',position=2,items=[{'id':11},{'id':12}],failures=[],
        settings={'configurations':{'outlook':{'rules':{}}}})
    failed = N(id=31,error='Synthetic failure',state='failed',quotes={})
    session = session_mock()
    session.get.return_value = N(enabled=True)
    session.scalar.side_effect = [None,None,job,None,None,failed]
    session.scalars.side_effect = [N(all=lambda:[failed] if stage == 'analysis' else []),
        N(all=lambda:[failed] if stage == 'events' else [])]
    monkeypatch.setattr(reprocess,'SessionLocal',lambda:session)
    monkeypatch.setattr(events,'reading_inputs',AsyncMock(return_value=[]))
    asyncio.run(reprocess.process_job())
    assert job.state == 'failed'
    assert job.failures == [{'stage':stage,'id':31,'error':'Synthetic failure'}]
    assert job.position == 2 and len(job.items) == 2


@pytest.mark.parametrize('stage', ['grouping', 'analysis', 'events', 'outlook'])
def test_retry_resumes_only_failed_stage(trading, stage):
    from alphapulse.api.risk import reprocess_retry
    job = N(id=10,state='failed',position=2,items=[{'id':11},{'id':12}],
        failures=[{'stage':stage,'id':11,'error':'Synthetic failure'}])
    failed = N(id=11,state='failed',quotes={})
    session = session_mock()
    session.get.return_value = job
    session.scalar.return_value = None
    session.scalars.side_effect = [N(all=lambda:[failed] if stage == 'outlook' else []),
        N(all=lambda:[failed] if stage == 'analysis' else []), N(all=lambda:[failed] if stage == 'events' else [])]
    asyncio.run(reprocess_retry(job.id,session))
    assert job.state == 'running' and job.failures == []
    if stage == 'grouping':
        assert job.items == [{'id':11, 'state':'pending'}] and job.position == 0
    else:
        assert job.items == [{'id':11},{'id':12}] and job.position == 2
        if stage == 'outlook': assert failed.quotes['retry_requested'] is True
        else: assert failed.state == 'pending' and failed.error is None


def test_job_outlook_retry_waits_for_its_grouping_retry(trading, monkeypatch):
    from alphapulse.news_risk import reprocess, worker, events
    session = session_mock()
    session.get.return_value = N(enabled=True)
    session.scalar.side_effect = [None, N(quotes={'job_id':10}), N(id=10,position=0,items=[{'id':11}])]
    monkeypatch.setattr(worker,'SessionLocal',lambda:session)
    monkeypatch.setattr(events,'reading_inputs',AsyncMock(return_value=[]))
    current = AsyncMock(side_effect=AssertionError('Must not use current settings for a job retry'))
    frozen = AsyncMock(side_effect=AssertionError('Must wait for grouping'))
    monkeypatch.setattr(worker,'configuration',current)
    monkeypatch.setattr(reprocess,'frozen_configuration',frozen)
    asyncio.run(worker.outlook())
    current.assert_not_awaited()
    frozen.assert_not_awaited()
    session.add.assert_not_called()
