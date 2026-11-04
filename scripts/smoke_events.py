"""Exercise atomic event and fact-package corrections in a disposable database."""
import asyncio
from datetime import UTC, datetime
import os
from urllib.parse import urlparse
from sqlalchemy import select, delete, func
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.models.risk import (NewsSummary, ArticleAnalysis, SummaryArticle, NewsEvent, EventMember,
    EventCorrectionRecord, PackageCorrection, EventVersion, SummaryVersion)
from alphapulse.news_risk import event_research, pipeline
from alphapulse.news_risk.research import Research
from alphapulse.news_risk.contracts import Correction
from alphapulse.news_risk.corrections import apply_correction


async def main():
    url=urlparse(os.environ['DATABASE_URL'])
    assert url.username=='ci' and url.password=='ci-only' and url.path=='/ci'
    now=datetime.now(UTC); packages=[]; events=[]; news_ids=[]
    try:
        async with SessionLocal() as session:
            for i in (1,2):
                news=NewsItem(source_code='wscn_news',external_id='ci-event-'+str(i),url='https://example.invalid/'+str(i),
                    title='Incident '+str(i),summary='',content_text='Original '+str(i),content_hash=str(i),published_at=now)
                session.add(news); await session.flush(); news_ids.append(news.id)
                analysis=ArticleAnalysis(news_id=news.id,content_hash=news.content_hash,state='done',input={
                    'title':news.title,'content':news.content_text,'published_at':now.isoformat(),'progress_at':now.isoformat()})
                session.add(analysis); await session.flush()
                package=NewsSummary(title=news.title,fact=news.title,category='安全',tags=['exchange'],entities=[],
                    published_at=now,updated_at=now,changed_at=now,due_at=now,revision=1,state='done')
                session.add(package); await session.flush(); packages.append(package.id)
                session.add(SummaryArticle(summary_id=package.id,analysis_id=analysis.id,active=True))
                await event_research.attach(session,package,pipeline.EventTarget(event_id=None,group_key=str(i),
                    title=news.title,fact=news.title,reason='Separate until verified'),{},True,False,None)
                await session.flush()
                membership=await session.get(EventMember,package.id); events.append(membership.event_id)
            await session.commit()
        async with SessionLocal() as session:
            original=[await session.get(NewsEvent,i) for i in events]
            proposal=event_research.EventCorrection(events=[{'id':e.id,'revision':e.revision} for e in original],
                replacements=[{'keep_id':min(events),'title':'Same concrete incident','fact':'Disclosure and response',
                    'package_ids':packages,'reason':'Explicit response to disclosure'}],reason='Verified common event')
            research=Research(); research.read_event_ids={max(events)}; research.read_event_versions={max(events):original[1].revision}
            # No reader ID is enough without the actual version observed by the tool.
            bad=Research(); bad.read_event_ids={max(events)}
            try: await event_research.correct(session,proposal,min(events),bad,{})
            except ValueError as error: assert '实际读取' in str(error)
            else: raise AssertionError('Unobserved correction version accepted')
            await event_research.correct(session,proposal,min(events),research,{})
            await session.commit()
            assert (await session.get(NewsEvent,max(events))).state=='retired'
            assert set(await session.scalars(select(EventMember.event_id)))=={min(events)}
            assert (await session.get(NewsEvent,min(events))).revision==2
            assert await session.scalar(select(func.count()).select_from(EventCorrectionRecord))==1
            current=await session.get(NewsEvent,min(events))
            split=event_research.EventCorrection(events=[{'id':current.id,'revision':current.revision}],replacements=[
                {'keep_id':current.id,'title':'One','fact':'One','package_ids':[packages[0]],'reason':'Split'},
                {'keep_id':None,'title':'Two','fact':'Two','package_ids':[packages[1]],'reason':'Split'}],reason='Repeat evidence')
            try: await event_research.correct(session,split,current.id,research,{})
            except ValueError as error: assert '反复拆合' in str(error)
            else: raise AssertionError('Same-source correction loop accepted')
            facts=[await session.get(NewsSummary,i) for i in packages]
            analyses=list(await session.scalars(select(ArticleAnalysis).where(ArticleAnalysis.news_id.in_(news_ids))))
            correction=Correction(packages=[{'id':p.id,'revision':p.revision} for p in facts],replacements=[{
                'keep_id':min(packages),'title':'Same fact','fact':'Same fact','category':'安全','tags':['exchange'],'content_kind':'report',
                'assignments':[{'analysis_id':a.id,'evidence':a.input['content']} for a in analyses]}],reason='Duplicate fact')
            outputs=await apply_correction(session,correction,min(packages),{max(packages)},{'test':True})
            await session.commit()
            assert outputs==[min(packages)]
            assert (await session.get(NewsSummary,max(packages))).replacement_ids==outputs
            assert (await session.get(NewsSummary,min(packages))).state=='done'
            assert (await session.get(NewsEvent,min(events))).state=='pending'
            assert await session.scalar(select(func.count()).select_from(SummaryVersion))==0
            assert await session.scalar(select(func.count()).select_from(EventVersion))==0
            assert await session.scalar(select(func.count()).select_from(NewsItem).where(NewsItem.id.in_(news_ids)))==2
        print('Corrections: single-package events, observed revisions, full assignment, canonical merge, repeat guard and fact-only package correction passed')
    finally:
        async with SessionLocal() as session:
            for model in (EventCorrectionRecord,PackageCorrection,EventMember,NewsEvent,SummaryArticle,NewsSummary,ArticleAnalysis):
                await session.execute(delete(model))
            await session.execute(delete(NewsItem).where(NewsItem.id.in_(news_ids))); await session.commit()
        await engine.dispose()


asyncio.run(main())
