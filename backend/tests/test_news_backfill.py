"""Backfill selection and task progress use isolated model and storage boundaries."""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock
import pytest


def test_progress_is_scoped_and_removed_on_failure():
    from alphapulse.news_risk.progress import execute, step, active, current
    async def check():
        async def task():
            step('查询资料',tool='read_news')
            assert len(active)==1 and current.get()['tool']=='read_news'
            raise ValueError('Synthetic')
        with pytest.raises(ValueError): await execute(('analysis',7),task)
        assert active=={} and current.get() is None
    asyncio.run(check())


@pytest.mark.parametrize('published,revisions,expected', [(True,(2,2),True),(True,(1,2),False),(False,(2,2),False)])
def test_all_current_packages_require_published_results(trading,published,revisions,expected):
    from alphapulse.news_risk.backfill import classification
    package=N(result={'summary':'ok'} if published else None,published_revision=revisions[0],revision=revisions[1])
    session=N(scalar=AsyncMock(return_value=N(id=3,result={'packages':[]})),scalars=AsyncMock(return_value=N(all=lambda:[package])))
    actual,_,_=asyncio.run(classification(session,N(id=1,content_hash='current')))
    assert actual is expected


def test_empty_active_memberships_are_incomplete(trading):
    from alphapulse.news_risk.backfill import classification
    session=N(scalar=AsyncMock(return_value=N(id=3,result={'old':True})),scalars=AsyncMock(return_value=N(all=lambda:[])))
    assert asyncio.run(classification(session,N(id=1,content_hash='current')))[0] is False


def test_expired_preview_does_not_create_work(trading,monkeypatch):
    from alphapulse.news_risk import backfill
    monkeypatch.setattr(backfill.redis,'get',AsyncMock(return_value=None))
    session=MagicMock()
    with pytest.raises(ValueError,match='预览已过期'):asyncio.run(backfill.start(session,'expired'))
    session.add.assert_not_called()


@pytest.mark.parametrize('modes',[[],['missing','missing'],['review','other'],['other']])
def test_range_rejects_empty_duplicate_or_unknown_modes(trading,modes):
    from pydantic import ValidationError
    from alphapulse.api.risk import ReprocessIn
    with pytest.raises(ValidationError):
        ReprocessIn(start='2026-10-01T00:00:00Z',end='2026-11-01T00:00:00Z',modes=modes)


@pytest.mark.parametrize('failure',[False,True])
def test_empty_or_invalid_outlook_poll_does_not_admit_a_task(trading,monkeypatch,failure):
    from alphapulse.news_risk import scheduler, worker
    session=MagicMock()
    session.__aenter__=AsyncMock(return_value=session); session.__aexit__=AsyncMock()
    session.get=AsyncMock(return_value=N(enabled=True,start_at='configured'))
    session.scalar=AsyncMock(return_value=None)
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:session)
    monkeypatch.setattr(worker,'enqueue',AsyncMock())
    check=AsyncMock(side_effect=ValueError('Synthetic configuration failure') if failure else None,return_value=False)
    monkeypatch.setattr(worker,'outlook',check)
    assert asyncio.run(scheduler.candidate(set(),0)) is None
    check.assert_awaited_once_with(prepare_only=True)
