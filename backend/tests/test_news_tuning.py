"""Workflow tuning preserves evidence boundaries and limits expensive work."""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock
import pytest


def test_keyword_queries_match_independently_and_escape_literals(trading):
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import dialect
    from alphapulse.models.risk import NewsSummary
    from alphapulse.news_risk.retrieval import terms_for, matching
    terms = terms_for('小鹏 交付 50%')
    condition, score = matching(NewsSummary, terms)
    query = select(NewsSummary.id).where(condition).order_by(score.desc()).compile(dialect=dialect())
    assert ' OR ' in str(query)
    assert any('50/%' in str(v) for v in query.params.values())
    assert terms_for('ignored', ['央行']) == ['央行','中国人民银行']
    with pytest.raises(ValueError): terms_for('   ')


def test_low_star_shortlist_rotates_domains_and_keeps_recent(trading):
    from alphapulse.news_risk.briefing import shortlist
    items = [{'id':i,'category':str(i%4),'importance_stars':2,'updated_at':f'2026-11-{i+1:02}'} for i in range(24)]
    chosen = shortlist(items)
    assert len(chosen) == 12
    assert len({p['category'] for p in chosen[:4]}) == 4
    assert chosen[0]['id'] == 23


def test_plan_time_is_distinct_from_occurrence(trading):
    from alphapulse.news_risk.contracts import PackageTimePoint
    point = dict(occurred_at=None,time_precision='unknown',time_text='明天',reported_at=None,
        description='拟发布',verification='partial',evidence_refs=[{'article_id':1,'quote':'明天'}])
    value=PackageTimePoint(**point,planned_at='2026-11-03T00:00:00+08:00',planned_precision='day',period_text='2026年10月')
    assert value.occurred_at is None and value.planned_at.day == 3
    with pytest.raises(ValueError): PackageTimePoint(**point,planned_precision='day')


def test_attempted_selection_needs_no_date_range(trading):
    from alphapulse.api.risk import ReprocessIn
    assert ReprocessIn(selection='attempted',modes=['review']).start is None
    with pytest.raises(ValueError): ReprocessIn(modes=['review'])


def test_exclusive_grouping_does_not_admit_realtime(trading,monkeypatch):
    from alphapulse.news_risk import scheduler,worker
    session=MagicMock()
    session.__aenter__=AsyncMock(return_value=session); session.__aexit__=AsyncMock()
    session.get=AsyncMock(return_value=N(enabled=True,start_at='configured'))
    job=N(id=7,settings={'request':{'run_mode':'history_only'}})
    session.scalar=AsyncMock(side_effect=[job,1,job])
    session.scalars=AsyncMock(return_value=N(all=lambda:[]))
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:session)
    enqueue=AsyncMock();outlook=AsyncMock()
    monkeypatch.setattr(worker,'enqueue',enqueue);monkeypatch.setattr(worker,'outlook',outlook)
    assert asyncio.run(scheduler.candidate(set(),0)) is None
    enqueue.assert_not_awaited();outlook.assert_not_awaited()


@pytest.mark.parametrize('snapshot_state', [None, 'processing', 'done', 'failed'])
def test_exclusive_completion_waits_for_reading_and_then_pauses(trading,snapshot_state,monkeypatch):
    from alphapulse.news_risk.backfill import reconcile
    from alphapulse.models.risk import ReprocessJob
    job=N(id=7,state='running',settings={'request':{'run_mode':'history_only'},'configurations':{'outlook':{'rules':{}}}},failures=[])
    from alphapulse.news_risk import events
    monkeypatch.setattr(events,'reading_inputs',AsyncMock(return_value=[]))
    settings=N(enabled=True)
    session=MagicMock()
    session.get=AsyncMock(side_effect=lambda model,*a,**k: job if model is ReprocessJob else settings)
    session.scalars=AsyncMock(return_value=N(all=lambda:[]))
    snapshot=None if snapshot_state is None else N(id=9,state=snapshot_state,error='Synthetic',quotes={})
    session.scalar=AsyncMock(side_effect=[None,snapshot])
    session.commit=AsyncMock()
    asyncio.run(reconcile(session,job))
    if snapshot_state in (None,'processing'):
        assert job.state=='running' and settings.enabled
    else:
        assert job.state==('done' if snapshot_state=='done' else 'failed')
        assert not settings.enabled


def test_expired_exclusive_preview_never_calls_paid_validation(trading,monkeypatch):
    from fastapi import HTTPException
    from alphapulse.api import risk
    from alphapulse.news_risk import backfill
    from alphapulse.core import db
    gate=MagicMock();gate.__aenter__=AsyncMock(return_value=gate);gate.__aexit__=AsyncMock(return_value=False)
    gate.scalar=AsyncMock(return_value=True);gate.commit=AsyncMock();gate.execute=AsyncMock()
    monkeypatch.setattr(db,'engine',N(connect=lambda:gate))
    monkeypatch.setattr(risk,'default_settings',AsyncMock(return_value=N(enabled=False)))
    monkeypatch.setattr(backfill,'prepare_start',AsyncMock(side_effect=ValueError('预览已过期')))
    validate=AsyncMock();monkeypatch.setattr(risk,'validate_enable',validate)
    with pytest.raises(HTTPException) as error:
        asyncio.run(risk.reprocess_start(risk.ReprocessStart(preview_id='a'*24,run_mode='history_only'),MagicMock()))
    assert error.value.status_code==409
    validate.assert_not_awaited()
    gate.execute.assert_awaited_once()


def test_ranking_only_skips_synthesis_and_synthesis_reuses_priority(trading,monkeypatch):
    from alphapulse.news_risk import briefing
    inputs=[{'id':1,'revision':2,'importance_stars':3,'result':{'summary':'Synthetic'}}]
    calls=[]
    async def complete(provider,system,payload,schema):
        calls.append(payload['task'])
        reading={'id':1,'revision':2,'priority':70,'reason':'Verified update'}
        if schema is briefing.Ranked: return schema(reading=[reading],notes='Synthetic'),{}
        trend={'summary':'Unknown','conditions':'Unknown','counter_evidence':'Unknown','watchpoints':'Unknown'}
        return schema(summary='Synthetic',market='Unknown',short_term=trend,medium_term=trend,
            references=[{**reading,'priority':99,'reason':'Changed'}]),{}
    monkeypatch.setattr(briefing,'complete',complete)
    provider=N(max_tokens=100,context_window=20000)
    ranked=asyncio.run(briefing.generate(provider,'rules',inputs,ranking_only=True))
    assert calls==['ranking']
    result=asyncio.run(briefing.generate(provider,'rules',inputs,rankings=ranked['reading']))
    assert calls==['ranking','synthesis']
    assert result['synthesis']['references']==ranked['reading']
