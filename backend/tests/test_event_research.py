"""Event-scoped research preserves fact coverage and amortizes repeated work."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock
import pytest


def test_event_debounce_has_hard_deadline_and_sticky_urgency(trading):
    from alphapulse.news_risk.event_research import schedule
    now=datetime.now(UTC)
    event=N(revision=0,state='pending',dirty_since=None,due_at=now,error=None)
    schedule(event,now)
    assert event.due_at==now+timedelta(minutes=5)
    for minute in (4,8,12,14): schedule(event,now+timedelta(minutes=minute))
    assert event.due_at==now+timedelta(minutes=15)
    event.state='done'; event.dirty_since=None
    schedule(event,now+timedelta(minutes=20),True)
    schedule(event,now+timedelta(minutes=20))
    assert event.due_at==now+timedelta(minutes=20)


@pytest.mark.parametrize('event_id,group_key',[(None,None),(1,'new')])
def test_stage_one_event_identity_is_exclusive(trading,event_id,group_key):
    from alphapulse.news_risk.pipeline import EventTarget
    with pytest.raises(ValueError): EventTarget(event_id=event_id,group_key=group_key,title='Title',fact='Fact',reason='Reason')


def test_pure_repost_updates_tags_without_research(trading):
    from alphapulse.models.risk import EventMember,NewsEvent
    from alphapulse.news_risk.event_research import attach
    now=datetime.now(UTC)
    event=N(id=2,state='done',tags=['old'],revision=3,updated_at=now,review_config=None)
    membership=N(event_id=2)
    session=N(get=AsyncMock(side_effect=lambda model,*a,**kw:membership if model is EventMember else event),add=MagicMock())
    target=N(event_id=2,group_key=None)
    asyncio.run(attach(session,N(id=1,tags=['new'],updated_at=now),target,{},False,False,None))
    assert event.revision==3 and event.state=='done' and event.tags==['new','old']
    assert session.get.call_args.args[0] is NewsEvent


def test_existing_package_cannot_be_moved_by_grouping(trading):
    from alphapulse.news_risk.event_research import attach
    session=N(get=AsyncMock(return_value=N(event_id=2)))
    with pytest.raises(ValueError,match='沿用主事件'):
        asyncio.run(attach(session,N(id=1),N(event_id=3),{},True,False,None))


@pytest.fixture
def event_database(trading):
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles, deregister
    from alphapulse.models.risk import NewsEvent, EventMember

    @compiles(JSONB, 'sqlite')
    def jsonb_sqlite(type_, compiler, **kw):
        return 'JSON'

    engine = create_engine('sqlite:///:memory:')
    try:
        NewsEvent.__table__.create(engine)
        EventMember.__table__.create(engine)
        yield engine
    finally:
        engine.dispose()
        deregister(JSONB)


@pytest.mark.parametrize('action', ['attach_new', 'attach_existing', 'dirty'])
@pytest.mark.parametrize('retired', [False, True])
def test_event_mutations_refresh_cached_versions_and_reject_retirement(event_database, action, retired):
    from sqlalchemy import select, update
    from sqlalchemy.orm import Session
    from alphapulse.models.risk import NewsEvent, EventMember
    from alphapulse.news_risk.event_research import attach
    from alphapulse.news_risk.events import dirty_event

    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(event_database) as seed:
        seed.add(NewsEvent(id=7, title='Incident', fact='Fact', tags=[], revision=10,
            published_revision=10, state='done', updated_at=now, due_at=now))
        if action != 'attach_new':
            seed.add(EventMember(package_id=1, event_id=7, reason='Original'))
        seed.commit()
    with Session(event_database, expire_on_commit=False) as first:
        cached = first.scalar(select(NewsEvent))
        first.commit()
        with Session(event_database) as writer:
            writer.execute(update(NewsEvent).where(NewsEvent.id == 7).values(
                revision=12, state='retired' if retired else 'done'))
            writer.commit()

        class Adapter:
            async def get(self, *args, **kwargs):
                return first.get(*args, **kwargs)

            def add(self, obj):
                first.add(obj)

            async def flush(self):
                first.flush()

        async def mutate():
            if action == 'dirty':
                await dirty_event(Adapter(), 1)
            else:
                await attach(Adapter(), N(id=1, tags=['new'], updated_at=now),
                    N(event_id=7, group_key=None, reason='Followup'), {}, True, True, None)

        if retired:
            with pytest.raises(ValueError, match='事件已失效'):
                asyncio.run(mutate())
        else:
            asyncio.run(mutate())
            assert cached.revision == 13 and cached.state == 'pending'
        first.commit()
    with Session(event_database) as check:
        event = check.get(NewsEvent, 7)
        assert (event.revision, event.state) == ((12, 'retired') if retired else (13, 'pending'))
        if retired and action == 'attach_new':
            assert check.get(EventMember, 1) is None


@pytest.mark.parametrize('package_config', [None, {'job_id': 98, 'request': {}}])
@pytest.mark.parametrize('keep_id', [1, None])
def test_package_correction_keeps_owning_event_frozen_configuration(trading, monkeypatch, package_config, keep_id):
    from alphapulse.models import NewsItem
    from alphapulse.models.risk import ArticleAnalysis, EventMember, NewsEvent, NewsSummary
    from alphapulse.news_risk import backfill
    from alphapulse.news_risk.contracts import Correction
    from alphapulse.news_risk.corrections import apply_correction

    now = datetime.now(UTC)
    config = {'job_id': 99, 'request': {'run_mode': 'history_only'}}
    package = N(id=1, revision=1, replacement_ids=[], fact='Original', tags=['old'], review_config=package_config)
    article = N(id=11, news_id=21, content_hash='hash', input={'content': 'Original',
        'published_at': now.isoformat(), 'progress_at': now.isoformat()})
    link = N(summary_id=1, analysis_id=11, active=True, unit={})
    membership = N(event_id=7)
    event = N(id=7, revision=1, state='processing', dirty_since=now, due_at=now, review_config=config)
    added = []

    async def get(model, identifier, **kwargs):
        if model is EventMember:
            return membership if identifier == 1 else None
        return {ArticleAnalysis: article, NewsItem: N(content_hash='hash'), NewsEvent: event}[model]

    async def flush():
        for obj in added:
            if isinstance(obj, NewsSummary) and obj.id is None:
                obj.id = 2

    session = N(scalars=AsyncMock(side_effect=[N(all=lambda: [package]), N(all=lambda: [link]), [7]]),
        scalar=AsyncMock(return_value=None), get=get, add=added.append, flush=flush, delete=AsyncMock())
    enroll = AsyncMock()
    monkeypatch.setattr(backfill, 'target', enroll)
    proposal = Correction(packages=[{'id': 1, 'revision': 1}], replacements=[{'keep_id': keep_id,
        'title': 'Corrected', 'fact': 'Original', 'category': '其他', 'tags': ['corrected'], 'content_kind': 'report',
        'assignments': [{'analysis_id': 11, 'evidence': 'Original'}]}], reason='Correct boundary')
    asyncio.run(apply_correction(session, proposal, 1, {1}, {}))
    assert event.state == 'pending' and event.revision == 2 and event.review_config == config
    enroll.assert_awaited_once_with(session, 99, 'events', event)
    output = package if keep_id else next(obj for obj in added if isinstance(obj, NewsSummary))
    assert output.review_config == config


@pytest.mark.parametrize('tool', ['read_event', 'read_package'])
def test_retry_refreshes_mutable_reads_and_caches_them_only_within_the_attempt(trading, monkeypatch, tool):
    from alphapulse.models.risk import NewsEvent, NewsSummary
    from alphapulse.news_risk import research

    reader = research.Research(event={'id': 7, 'revision': 1})
    reader.restore([{'tool': tool, 'arguments': {'id': 2}, 'result': {'id': 2, 'revision': 1}}])
    assert not reader.read_event_ids and not reader.read_package_ids
    row = N(id=2, revision=2, title='Current', fact='Current', tags=[], result=None, replacement_ids=[], state='done')
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = AsyncMock(side_effect=lambda model, *a, **kw: row if model in (NewsEvent, NewsSummary) else None)
    empty = MagicMock()
    empty.all.return_value = []
    empty.__iter__.return_value = iter([])
    session.scalars = AsyncMock(return_value=empty)
    storage = MagicMock(return_value=session)
    monkeypatch.setattr(research, 'SessionLocal', storage)

    async def check():
        first = await reader.tool(tool, {'id': 2})
        assert first['revision'] == 2
        assert await reader.tool(tool, {'id': 2}) == first

    asyncio.run(check())
    assert reader.counts['read'] == 1 and len(reader.records) == 1
    storage.assert_called_once_with()
    if tool == 'read_event':
        assert reader.read_event_versions == {2: 2}
    else:
        assert reader.read_package_ids == {2}


def result(trading,package_ids=(1,2)):
    from alphapulse.news_risk.event_research import EventResult
    refs=[{'article_id':i,'quote':f'Original {i}'} for i in package_ids]
    return EventResult(id=7,title='Incident',summary='Summary',category='安全',entities=[],risk_stars=2,importance_stars=3,
        direction='unknown',verification='insufficient',confidence='low',reasoning='Unknown',uncertainty='Unknown',
        facts='Facts',timeline='Timeline',asset_impacts='Unknown',opinions='Unknown',counter_evidence='Unknown',watchpoints='Updates',
        claims=[{'statement':'Claim','verdict':'insufficient','evidence_refs':[]}],evidence_refs=refs,
        timeline_points=[{'package_id':i,'package_revision':1,'occurred_at':None,'time_precision':'unknown','time_text':'',
            'reported_at':None,'description':'Fact','verification':'partial','evidence_refs':[r]} for i,r in zip(package_ids,refs,strict=True)])


@pytest.mark.parametrize('fault',['omitted','revision','wrong_member','invented_quote','direction'])
def test_event_validation_covers_each_member_with_its_own_original(trading,fault):
    from alphapulse.news_risk.event_research import validate
    output=result(trading)
    packages=[{'id':i,'revision':1,'article_ids':[i]} for i in (1,2)]
    sources={i:{'content':f'Original {i}'} for i in (1,2)}
    if fault=='omitted':output.timeline_points.pop()
    if fault=='revision':output.timeline_points[0].package_revision=2
    if fault=='wrong_member':output.timeline_points[0].evidence_refs=[{'article_id':2,'quote':'Original 2'}]
    if fault=='invented_quote':output.evidence_refs[0].quote='Invented'
    if fault=='direction':output.direction='bullish'
    with pytest.raises(ValueError):validate(output,packages,sources)


def test_event_schema_has_no_per_package_research_or_event_proposal(trading):
    from alphapulse.news_risk.event_research import EventResult
    fields=EventResult.model_fields
    assert 'event_proposal' not in fields and 'related_packages' not in fields
    assert 'package_correction' in fields and 'correction' in fields


def test_self_event_and_supplied_package_reads_do_not_use_storage(trading,monkeypatch):
    from alphapulse.news_risk import research
    guard=MagicMock(side_effect=AssertionError('Supplied inputs must not trigger storage'))
    monkeypatch.setattr(research,'SessionLocal',guard)
    reader=research.Research(event={'id':7,'revision':2})
    reader.provided_packages={1:{'id':1,'revision':1,'fact':'Provided'}}
    async def check():
        assert (await reader.tool('read_event',{'id':7}))['status']=='already_provided'
        assert (await reader.tool('read_package',{'id':1}))['status']=='already_provided'
    asyncio.run(check());guard.assert_not_called()
    assert reader.counts['read']==0


def test_grouping_budget_keeps_both_package_and_event_candidates(trading):
    from alphapulse.news_risk.pipeline import grouping_payload,Grouping,fits_context
    provider=N(max_tokens=1000,context_window=14000)
    candidates=[{'id':i,'fact':'p'*500} for i in range(50)]
    events=[{'id':i,'fact':'e'*500} for i in range(50)]
    payload,_=asyncio.run(grouping_payload(provider,'rules',{'id':1,'title':'Original','content':'Original'},candidates,events))
    assert payload['candidates'] and payload['events']
    assert len(payload['candidates'])<50 and len(payload['events'])<50
    assert fits_context(provider,'rules',payload,Grouping)


def test_long_event_rejects_extraction_that_loses_an_original(trading,monkeypatch):
    from alphapulse.news_risk import event_research as module
    reader=N(fits=lambda *a:False,run=AsyncMock())
    monkeypatch.setattr(module,'extract_articles',AsyncMock(return_value=([{'facts':'One','evidence_refs':[{'article_id':1,'quote':'One'}]}],[{'input_tokens':9}])))
    with pytest.raises(ValueError,match='遗漏事件原文') as error:
        asyncio.run(module.generate(N(),'rules',{'articles':[{'id':1},{'id':2}],'packages':[]},reader))
    assert error.value.usage['parts']==[{'input_tokens':9}]
    reader.run.assert_not_awaited()


def test_long_event_extracts_every_original_and_keeps_all_member_ids(trading,monkeypatch):
    from alphapulse.news_risk import event_research as module, pipeline
    from alphapulse.news_risk.research import Research
    provider=N(max_tokens=1500,context_window=15000)
    reader=Research()
    output=result(trading)
    articles=[{'id':i,'title':f'Original {i}','content':'x'*20000,'summary':''} for i in (1,2)]
    inputs=[{'id':i,'revision':1,'article_ids':[i]} for i in (1,2)]
    async def complete(provider,system,payload,schema):
        if payload['task']=='extract':
            return pipeline.Extraction(facts='Extracted',evidence_refs=[{'article_id':payload['article_id'],'quote':payload['content'][:1]}]),{'input_tokens':1}
        raise AssertionError('Small extracts do not need another merge')
    async def research(provider,system,payload,schema):
        assert {p['id'] for p in payload['packages']}=={1,2}
        assert {r['article_id'] for e in payload['extractions'] for r in e['evidence_refs']}=={1,2}
        assert 'articles' not in payload
        return output,{'rounds':[{'output_tokens':5}]}
    monkeypatch.setattr(pipeline,'complete',complete)
    monkeypatch.setattr(reader,'run',research)
    actual,usage=asyncio.run(module.generate(provider,'rules',{'id':7,'event':{'id':7},'articles':articles,'packages':inputs},reader))
    assert actual is output and len(usage['parts'])>=2 and usage['final']['rounds'][0]['output_tokens']==5


def test_derived_reset_allowlist_protects_originals_and_configuration(trading):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/reset_news_ai.py'
    spec=importlib.util.spec_from_file_location('reset_news_ai_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    names={model.__tablename__ for model in module.DERIVED}
    assert not names & {'news_items','news_revisions','risk_settings','ai_providers','skill_nodes','skill_versions','chat_sessions','economic_releases'}
    assert {'news_ai_events','news_ai_summaries','news_ai_event_evidence','news_ai_reprocess_jobs','risk_analyses'}<=names
    report={'real_models':True,'quality_accepted':True,'baseline_sha256':module.hashlib.sha256(b'baseline').hexdigest(),
        'baseline_packages':20,'originals':27,'failed':0,'unassigned':0}
    assert module.accepted(report,b'baseline')
    for changes in ({'real_models':False},{'quality_accepted':False},{'failed':1},{'unassigned':1},{'originals':26}):
        assert not module.accepted({**report,**changes},b'baseline')
    assert not module.accepted(report,b'other')


def test_usage_accumulates_retries_without_double_counting(trading):
    from alphapulse.news_risk.service import accumulate_usage
    first={'parts':[{'input_tokens':4}],'final':{'output_tokens':2}}
    second={'rounds':[{'input_tokens':5}]}
    usage=accumulate_usage(None,first)
    usage=accumulate_usage(usage,second)
    assert usage=={'attempts':[first,second]}
    assert accumulate_usage({'input_tokens':7},second)=={'attempts':[{'input_tokens':7},second]}


def test_updating_events_do_not_erase_last_successful_briefing(trading,monkeypatch):
    from alphapulse.news_risk import worker,events
    session=MagicMock()
    session.__aenter__=AsyncMock(return_value=session);session.__aexit__=AsyncMock(return_value=False)
    session.get=AsyncMock(return_value=N(enabled=True))
    session.scalar=AsyncMock(side_effect=[N(state='done'),None,None,7])
    monkeypatch.setattr(worker,'SessionLocal',lambda:session)
    monkeypatch.setattr(events,'reading_inputs',AsyncMock(return_value=[]))
    model=AsyncMock(side_effect=AssertionError('Wait for event research'))
    monkeypatch.setattr(worker,'configuration',model)
    assert asyncio.run(worker.outlook(prepare_only=True)) is None
    session.add.assert_not_called();model.assert_not_awaited()
