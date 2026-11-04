"""Workflow tuning preserves evidence boundaries and limits expensive work."""
import asyncio
from datetime import UTC, datetime
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


def test_large_synthesis_compacts_package_notes_in_batches(trading,monkeypatch):
    import json
    from alphapulse.news_risk import briefing
    inputs=[{'id':i,'revision':1,'importance_stars':3,'result':{'summary':'x'*800}} for i in range(3)]
    rankings=[{'kind':'package','id':i,'revision':1,'priority':50,'reason':'Read'} for i in range(3)]
    calls=[]
    def fits(provider,system,payload,schema): return len(json.dumps(payload)) < 1600
    async def complete(provider,system,payload,schema):
        calls.append(payload['task'])
        if schema is briefing.Notes: return schema(notes='Compacted'),{}
        trend={'summary':'Unknown','conditions':'Unknown','counter_evidence':'Unknown','watchpoints':'Unknown'}
        return schema(summary='Synthetic',market='Unknown',short_term=trend,medium_term=trend,references=[]),{}
    monkeypatch.setattr(briefing,'fits_context',fits);monkeypatch.setattr(briefing,'complete',complete)
    result=asyncio.run(briefing.generate(N(),'rules',inputs,rankings=rankings))
    assert calls==['synthesis_notes']*3+['synthesis']
    assert len(result['reading'])==3


@pytest.mark.parametrize('original_start', [None, datetime(2026,11,1,tzinfo=UTC)])
def test_exclusive_start_persists_dispatch_start_and_preserves_existing_boundary(trading,monkeypatch,original_start):
    from alphapulse.news_risk import backfill,scheduler
    settings=N(enabled=False,start_at=original_start)
    session=MagicMock()
    session.get=AsyncMock(return_value=settings);session.flush=AsyncMock();session.commit=AsyncMock()
    session.add=lambda job:setattr(job,'id',7)
    value={'items':[{'id':1,'hash':'current','mode':'review'}],'config':{},'request':{'selection':'range'}}
    monkeypatch.setattr(backfill,'prepare_start',AsyncMock(return_value=value))
    monkeypatch.setattr(backfill.redis,'delete',AsyncMock())
    before=datetime.now(UTC)
    result=asyncio.run(backfill.start(session,'preview','history_only',validated=True))
    assert result=={'id':7} and settings.enabled
    if original_start is None: assert before<=settings.start_at<=datetime.now(UTC)
    else: assert settings.start_at==original_start
    session.__aenter__=AsyncMock(return_value=session);session.__aexit__=AsyncMock(return_value=False)
    job=N(id=7,settings={'request':{'run_mode':'history_only'}})
    session.scalar=AsyncMock(side_effect=[job,1,job,None])
    session.scalars=AsyncMock(return_value=N(all=lambda:[N(id=1,news_id=1,content_hash='current')]))
    monkeypatch.setattr(scheduler,'SessionLocal',lambda:session)
    picked=asyncio.run(scheduler.candidate(set(),0))
    assert picked[0]==('backfill',1)


@pytest.mark.parametrize('event_state', ['pending','processing','failed'])
def test_exclusive_batch_tracks_event_without_package_research(trading,monkeypatch,event_state):
    from alphapulse.news_risk import backfill
    from alphapulse.models.risk import ReprocessJob,NewsEvent,RiskSettings
    config={'request':{'run_mode':'history_only'}}
    job=N(id=7,state='running',settings=config,failures=[])
    event=N(id=9,state=event_state,published_revision=0,error='Synthetic')
    settings=N(enabled=True)
    session=MagicMock()
    session.get=AsyncMock(side_effect=lambda model,*a,**k:{ReprocessJob:job,NewsEvent:event,RiskSettings:settings}[model])
    session.scalar=AsyncMock(return_value=None)
    session.scalars=AsyncMock(side_effect=[N(all=lambda:[]),N(all=lambda:[N(kind='events',object_id=9,revision=1)])])
    session.commit=AsyncMock()
    enroll=AsyncMock();monkeypatch.setattr(backfill,'target',enroll)
    asyncio.run(backfill.reconcile(session,job))
    enroll.assert_not_awaited()
    assert job.state==('failed' if event_state=='failed' else 'running')
    assert settings.enabled == (event_state!='failed')


def test_exclusive_batch_finishes_after_event_dependency_is_published(trading,monkeypatch):
    from alphapulse.news_risk import backfill,events
    from alphapulse.models.risk import ReprocessJob,NewsEvent,NewsSummary,RiskSettings,EventMember
    config={'request':{'run_mode':'history_only'},'configurations':{'outlook':{'rules':{}}}}
    job=N(id=7,state='running',settings=config,failures=[])
    event=N(id=9,state='done',published_revision=1)
    dependency=N(id=2,state='done',revision=2,published_revision=2,replacement_ids=[])
    targets=[N(kind='events',object_id=9,revision=1),N(kind='analysis',object_id=2,revision=2)]
    settings=N(enabled=True)
    session=MagicMock()
    session.get=AsyncMock(side_effect=lambda model,*a,**k:{
        ReprocessJob:job,NewsEvent:event,NewsSummary:dependency,RiskSettings:settings,EventMember:None}[model])
    session.scalar=AsyncMock(side_effect=[None,N(id=3,state='done',quotes={})])
    session.scalars=AsyncMock(side_effect=[N(all=lambda:[]),N(all=lambda:targets),N(all=lambda:[])])
    session.commit=AsyncMock()
    monkeypatch.setattr(events,'reading_inputs',AsyncMock(return_value=[]))
    asyncio.run(backfill.reconcile(session,job))
    assert job.state=='done' and not settings.enabled


@pytest.mark.parametrize('failed_stage', ['grouping','events',None])
@pytest.mark.parametrize('prepare_only', [True,False])
def test_exclusive_outlook_never_admits_failed_batch_results(trading,monkeypatch,failed_stage,prepare_only):
    from alphapulse.news_risk import worker,events,reprocess
    from alphapulse.models.risk import ReprocessJob,ReprocessItem,NewsSummary,NewsEvent
    job=N(id=7,state='running',position=0,items=[],settings={'request':{'run_mode':'history_only'}})
    failed_model={'grouping':ReprocessItem,'analysis':NewsSummary,'events':NewsEvent}.get(failed_stage)
    session=MagicMock()
    session.__aenter__=AsyncMock(return_value=session);session.__aexit__=AsyncMock(return_value=False)
    session.get=AsyncMock(return_value=N(enabled=True));session.commit=AsyncMock()
    async def scalar(query):
        model=query.column_descriptions[0]['entity']
        if model is ReprocessJob: return job
        if model is failed_model:
            # Emulate filtering a stored failed row, without storage or model I/O.
            return 1 if any(isinstance(value,list) and 'failed' in value for value in query.compile().params.values()) else None
        return None
    session.scalar=AsyncMock(side_effect=scalar)
    inputs=[{'id':1,'kind':'package','revision':1,'importance_stars':3,'category':'company','result':{'summary':'Previous successful result'}}]
    monkeypatch.setattr(worker,'SessionLocal',lambda:session)
    monkeypatch.setattr(events,'reading_inputs',AsyncMock(return_value=inputs))
    configuration=AsyncMock(return_value=(N(model='advanced'),'rules',{}))
    monkeypatch.setattr(reprocess,'frozen_configuration',configuration)
    generate=AsyncMock(return_value={'reading':[],'synthesis':None,'usage':[]})
    monkeypatch.setattr(worker.briefing,'generate',generate)
    ready=asyncio.run(worker.outlook(prepare_only=prepare_only))
    if failed_stage:
        assert ready is None
        configuration.assert_not_awaited();generate.assert_not_awaited();session.add.assert_not_called()
    elif prepare_only:
        assert ready is True
        generate.assert_not_awaited()
    else:
        assert generate.await_count==2
        assert session.add.call_args.args[0].state=='done'


@pytest.mark.parametrize('case', ['valid','wrong_date','wrong_text','wrong_quote','extra_evidence'])
def test_event_plan_binds_date_precision_text_and_its_original_quote(trading,case):
    from alphapulse.news_risk.events import EventResult,validate_timeline
    def point(day):
        return dict(occurred_at=None,time_precision='unknown',planned_at=f'2026-11-{day}T00:00:00Z',
            planned_precision='day',time_text=f'11月{day}日',reported_at=None,description='Plan',verification='partial',
            evidence_refs=[{'article_id':1,'quote':f'计划11月{day}日开始'}])
    first,second=point('10'),point('20')
    submitted={**first,'package_id':1,'package_revision':1}
    if case=='wrong_date': submitted={**second,'planned_at':first['planned_at'],'package_id':1,'package_revision':1}
    if case=='wrong_text': submitted['time_text']=second['time_text']
    if case=='wrong_quote': submitted['evidence_refs']=second['evidence_refs']
    if case=='extra_evidence': submitted['evidence_refs']=first['evidence_refs']+second['evidence_refs']
    result=EventResult(title='Synthetic',summary='Synthetic',category='company',importance_stars=3,risk_stars=0,
        uncertainty='Unknown',opinions='Unknown',counter_evidence='Unknown',watchpoints='Unknown',timeline_points=[submitted])
    packages=[{'id':1,'revision':1,'result':{'timeline_points':[first,second]}}]
    if case in ('valid','extra_evidence'): validate_timeline(result,packages)
    else:
        with pytest.raises(ValueError,match='已核验计划'): validate_timeline(result,packages)
