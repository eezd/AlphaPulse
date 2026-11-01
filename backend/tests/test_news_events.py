"""Event identity, timeline provenance and heterogeneous reading contracts."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def point(**changes):
    return {'occurred_at':None,'time_precision':'unknown','time_text':'未说明','reported_at':None,
        'description':'Exchange response','verification':'partial','package_id':1,'package_revision':2,
        'evidence_refs':[{'article_id':10,'quote':'response'}], **changes}


@pytest.mark.parametrize('changes', [{'package_revision':3}, {'package_id':2}, {'evidence_refs':[{'article_id':10,'quote':'invented'}]}])
def test_event_timeline_rejects_unknown_versions_and_invented_quotes(trading, changes):
    from alphapulse.news_risk.events import EventResult, validate_timeline
    result=EventResult(title='Incident',summary='Summary',category='security',importance_stars=4,risk_stars=3,
        uncertainty='Unconfirmed',opinions='Wait',counter_evidence='Unknown',watchpoints='Response',timeline_points=[point(**changes)])
    with pytest.raises(ValueError):
        validate_timeline(result,[{'id':1,'revision':2,'result':{'evidence_refs':[{'article_id':10,'quote':'response'}]}}])


@pytest.mark.parametrize('changes', [{'occurred_at':'2026-11-01T12:00:00Z'}, {'time_precision':'day'},
    {'occurred_at':'2026-11-01T12:00:00','time_precision':'minute'}])
def test_time_precision_never_invents_occurrence_time(trading, changes):
    from alphapulse.news_risk.events import TimelinePoint
    with pytest.raises(ValueError): TimelinePoint(**point(**changes))


def test_reading_distinguishes_event_and_package_with_same_id(trading, monkeypatch):
    from alphapulse.news_risk import briefing
    inputs=[{'kind':kind,'id':1,'revision':1,'importance_stars':3,'result':{'summary':kind}} for kind in ('event','package')]
    async def complete(provider, system, payload, schema):
        if schema is briefing.Ranked:
            return schema(reading=[{'kind':p['kind'],'id':1,'revision':1,'priority':50,'reason':'Read'} for p in payload['packages']],notes='Notes'),{}
        trend=briefing.Trend(summary='Unknown',conditions='Unknown',counter_evidence='Unknown',watchpoints='Updates')
        return schema(summary='Summary',market='Unknown',short_term=trend,medium_term=trend,references=[]),{}
    monkeypatch.setattr(briefing,'complete',complete)
    result=asyncio.run(briefing.generate(SimpleNamespace(max_tokens=100,context_window=20000),'rules',inputs))
    assert {r['kind'] for r in result['reading']}=={'event','package'}


def test_members_are_aggregated_once_and_old_independent_package_expires(trading):
    from alphapulse.news_risk.events import reading_inputs
    now=datetime.now(UTC)
    packages=[SimpleNamespace(id=i,updated_at=now if i<3 else now-timedelta(days=4),importance_stars=4,
        result={'title':str(i)},published_revision=2,category='security',tags=[]) for i in (1,2,3)]
    event=SimpleNamespace(id=1,published_revision=2,result={'title':'Incident','importance_stars':2,'category':'security','timeline_points':[{'package_id':i} for i in (1,2)]})
    session=AsyncMock()
    values=[packages,[SimpleNamespace(package_id=i,event_id=1) for i in (1,2)],[event]]
    session.scalars.side_effect=[SimpleNamespace(all=lambda rows=rows:rows) for rows in values]
    session.scalar.return_value=SimpleNamespace(revision=2)
    inputs=asyncio.run(reading_inputs(session,now-timedelta(hours=72)))
    assert len(inputs)==1 and inputs[0]['kind']=='event' and inputs[0]['importance_stars']==4


def test_reprocess_range_requires_order_and_timezone(trading):
    from alphapulse.api.risk import ReprocessIn
    with pytest.raises(ValueError): ReprocessIn(start='2026-11-01',end='2026-11-02')
    with pytest.raises(ValueError): ReprocessIn(start='2026-11-02T00:00:00Z',end='2026-11-01T00:00:00Z')


def test_event_timeline_cannot_omit_a_member_or_invent_known_times(trading):
    from alphapulse.news_risk.events import EventResult, validate_timeline
    result=EventResult(title='Incident',summary='Summary',category='security',importance_stars=4,risk_stars=3,
        uncertainty='Unconfirmed',opinions='Wait',counter_evidence='Unknown',watchpoints='Response',timeline_points=[point()])
    packages=[{'id':i,'revision':2,'result':{'evidence_refs':[{'article_id':10,'quote':'response'}]}} for i in (1,2)]
    with pytest.raises(ValueError, match='覆盖每个组成包'): validate_timeline(result,packages)
    result.timeline_points[0].occurred_at=datetime.now(UTC)
    result.timeline_points[0].time_precision='minute'
    with pytest.raises(ValueError, match='已核验时间点'): validate_timeline(result,packages[:1])


def test_new_member_remains_readable_until_event_represents_it(trading):
    from alphapulse.news_risk.events import reading_inputs
    now=datetime.now(UTC)
    packages=[SimpleNamespace(id=i,updated_at=now,importance_stars=4,result={'title':str(i)},published_revision=1,
        category='security',tags=[]) for i in (1,2,3)]
    event=SimpleNamespace(id=1,published_revision=1,result={'title':'Incident','importance_stars':4,'category':'security',
        'timeline_points':[{'package_id':1},{'package_id':2}]})
    session=AsyncMock()
    rows=[packages,[SimpleNamespace(package_id=i,event_id=1) for i in (1,2,3)],[event]]
    session.scalars.side_effect=[SimpleNamespace(all=lambda rows=rows:rows) for rows in rows]
    inputs=asyncio.run(reading_inputs(session,now-timedelta(hours=72)))
    assert [(p['kind'],p['id']) for p in inputs]==[('event',1),('package',3)]


@pytest.mark.parametrize('withdraw_during_call', [False, True])
def test_withdrawn_member_allows_publish_without_repeated_calls(trading, monkeypatch, withdraw_during_call):
    from unittest.mock import MagicMock
    from alphapulse.news_risk import events
    from alphapulse.models.risk import RiskSettings
    now = datetime.now(UTC)
    members = [SimpleNamespace(id=i, published_revision=2, published_at=now, replacement_ids=[],
        state='done', result={'evidence_refs':[{'article_id':10,'quote':'response'}]}) for i in (1,2,3)]
    if not withdraw_during_call: members[2].state = 'withdrawn'
    event = SimpleNamespace(id=1,revision=2,published_revision=1,result=None,review_config=None,state='pending')
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = AsyncMock(side_effect=lambda model,*a,**kw: SimpleNamespace(enabled=True) if model is RiskSettings else event)
    session.scalar = AsyncMock(side_effect=lambda *a: event if event.state == 'pending' else None)
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda:members))
    session.commit = AsyncMock()
    session.rollback = AsyncMock(side_effect=AssertionError('Unexpected rollback'))
    async def complete(provider, system, payload, schema):
        members[2].state = 'withdrawn'
        return schema(title='Incident',summary='Summary',category='security',importance_stars=4,risk_stars=3,
            uncertainty='Unconfirmed',opinions='Wait',counter_evidence='Unknown',watchpoints='Response',
            timeline_points=[point(package_id=p['id']) for p in payload['packages']]), {}
    model = AsyncMock(side_effect=complete)
    monkeypatch.setattr(events,'SessionLocal',lambda:session)
    monkeypatch.setattr(events,'configuration',AsyncMock(return_value=(SimpleNamespace(),'rules',{},'fp')))
    monkeypatch.setattr(events,'complete',model)
    async def check():
        await events.organize()
        if withdraw_during_call:
            assert event.state == 'pending' and event.published_revision == 1
        await events.organize()
        await events.organize()
    asyncio.run(check())
    assert event.state == 'done' and event.published_revision == 2
    assert {p['package_id'] for p in event.result['timeline_points']} == {1,2}
    assert model.await_count == (2 if withdraw_during_call else 1)
