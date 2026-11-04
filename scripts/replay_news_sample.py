"""Run the fixed baseline only in an empty, disposable CI database.

Credentials are supplied explicitly in a private JSON file. This script never
reads live configuration; reusing a live provider requires explicit operator
authorization. The providers map has grouping/analysis/outlook entries containing
protocol, base_url and api_key. No quality-acceptance flag is set automatically.
"""
import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from sqlalchemy import select, func
from alphapulse.core.crypto import encrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem
from alphapulse.models.risk import RiskSettings, ArticleAnalysis, NewsSummary, NewsEvent, EventMember, EventVersion, RiskSnapshot
from alphapulse.news_risk import worker, pipeline, event_research
from alphapulse.ai.skills import create_node, add_version


async def bounded(identifiers, action, limit=4):
    pending=iter(identifiers)
    tasks=set()
    try:
        while tasks or pending is not None:
            while pending is not None and len(tasks)<limit:
                try: identifier=next(pending)
                except StopIteration: pending=None; break
                tasks.add(asyncio.create_task(action(identifier)))
            if tasks:
                done,tasks=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                results=await asyncio.gather(*done,return_exceptions=True)
                for result in results:
                    if isinstance(result,BaseException): raise result
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def tokens(value):
    if isinstance(value,list):
        result={}
        for item in value:
            for k,v in tokens(item).items(): result[k]=result.get(k,0)+v
        return result
    if not isinstance(value,dict): return {}
    result={k:v for k,v in value.items() if k in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens') and isinstance(v,(int,float))}
    for item in value.values():
        if isinstance(item,(dict,list)):
            for k,v in tokens(item).items(): result[k]=result.get(k,0)+v
    return result


async def replay(sample_path, credentials_path, output, news_ids=None, supplement=None):
    url=urlparse(os.environ['DATABASE_URL'])
    if not (url.username=='ci' and url.password=='ci-only' and url.path=='/ci' and
            (url.hostname in ('localhost','127.0.0.1') or url.hostname.startswith('ap-ci-'))):
        raise ValueError('样本只能写入独立CI数据库')
    baseline=sample_path.read_bytes(); sample=json.loads(baseline)
    if len(sample['packages'])!=20 or len(sample['news'])!=27: raise ValueError('不是已固定的20包27原文基线')
    supplement_hash=None
    if supplement:
        extra=supplement.read_bytes(); supplement_hash=hashlib.sha256(extra).hexdigest()
        existing={n['id']:n for n in sample['news']}
        for item in json.loads(extra):
            if item['id'] in existing:
                if item['content_hash']!=existing[item['id']]['content_hash']: raise ValueError('补充样本与固定原文冲突')
            else: existing[item['id']]=item
        sample['news']=sorted(existing.values(),key=lambda n:(n['published_at'],n['id']))
    if news_ids:
        selected=set(news_ids)
        if not selected <= {n['id'] for n in sample['news']}: raise ValueError('定向ID必须来自固定基线')
        sample['news']=[n for n in sample['news'] if n['id'] in selected]
    credentials=json.loads(credentials_path.read_text())
    started=time.monotonic()
    ids={}; skills={}
    try:
        async with SessionLocal() as session:
            if await session.scalar(select(func.count()).select_from(NewsItem)) or await session.get(RiskSettings,1):
                raise ValueError('验收数据库必须为空')
            for stage in ('grouping','analysis','outlook'):
                config=credentials['providers'][stage]
                provider=AiProvider(name='sample-'+stage,protocol=config['protocol'],base_url=config['base_url'],
                    api_key_encrypted=encrypt(config['api_key']),api_key_hint='sample',model='sample',max_tokens=config.get('max_tokens',16384),context_window=config.get('context_window',200000))
                session.add(provider); await session.flush(); ids[stage]=provider.id
                name,content=pipeline.TEMPLATES[stage]
                node=await create_node(session,None,'skill',name,scope='news')
                version=await add_version(session,node,content,'user','Isolated sample')
                node.published_version_id=version.id; skills[stage+'_skill_id']=node.id
            model=sample['model_settings']
            settings=RiskSettings(id=1,enabled=True,start_at=datetime.fromisoformat(sample['start']),max_concurrency=4,
                provider_id=ids['grouping'],second_provider_id=ids['analysis'],third_provider_id=ids['outlook'],**skills,
                **{k:model[k] for k in ('model','second_model','third_model','reasoning_effort','second_reasoning_effort','third_reasoning_effort')})
            session.add(settings)
            for item in sample['news']:
                session.add(NewsItem(id=item['id'],external_id='sample-'+str(item['id']),**{k:v for k,v in item.items() if k not in ('id','published_at','source_updated_at')},
                    published_at=datetime.fromisoformat(item['published_at']),source_updated_at=datetime.fromisoformat(item['source_updated_at']) if item['source_updated_at'] else None))
            await session.commit()
        for _attempt in range(30):
            async with SessionLocal() as session:
                await worker.enqueue(session,await session.get(RiskSettings,1))
                pending=list(await session.scalars(select(ArticleAnalysis.id).where(ArticleAnalysis.state=='pending')))
            if not pending: break
            await bounded(pending,worker.analyze)
        async with SessionLocal() as session:
            # Drain the historical sample after grouping, without real-time arrivals.
            events=list(await session.scalars(select(NewsEvent).where(NewsEvent.state=='pending')))
            for event in events: event.due_at=datetime.now(UTC)
            await session.commit()
        for _attempt in range(10):
            async with SessionLocal() as session:
                pending=list(await session.scalars(select(NewsEvent.id).where(NewsEvent.state=='pending')))
                for event in list(await session.scalars(select(NewsEvent).where(NewsEvent.id.in_(pending)))): event.due_at=datetime.now(UTC)
                await session.commit()
            if not pending: break
            await bounded(pending,event_research.review)
        async with SessionLocal() as session:
            unfinished_articles=await session.scalar(select(func.count()).select_from(ArticleAnalysis).where(ArticleAnalysis.state!='done'))
            unfinished_events=await session.scalar(select(func.count()).select_from(NewsEvent).where(NewsEvent.state.notin_(['done','retired'])))
        reading_generated=not (unfinished_articles or unfinished_events)
        if reading_generated: await worker.outlook()
        async with SessionLocal() as session:
            articles=list(await session.scalars(select(ArticleAnalysis)))
            packages=list(await session.scalars(select(NewsSummary).where(NewsSummary.replacement_ids==[])))
            events=list(await session.scalars(select(NewsEvent).where(NewsEvent.state!='retired')))
            versions=list(await session.scalars(select(EventVersion)))
            snapshots=list(await session.scalars(select(RiskSnapshot)))
            assigned=set(await session.scalars(select(EventMember.package_id)))
            report={'real_models':True,'quality_accepted':False,'supplement_sha256':supplement_hash,'baseline_sha256':hashlib.sha256(baseline).hexdigest(),
                'skill_hashes':{stage:hashlib.sha256(content.encode()).hexdigest() for stage,(_,content) in pipeline.TEMPLATES.items()},
                'model_settings':sample['model_settings'],'test_max_concurrency':4,
                'baseline_packages':20,'originals':len(sample['news']),'selected_news_ids':[n['id'] for n in sample['news']],'reading_generated':reading_generated,'packages':len(packages),'events':len(events),
                'unassigned':len({p.id for p in packages}-assigned),
                'failed':sum(a.state!='done' for a in articles)+sum(e.state!='done' for e in events)+sum(s.state!='done' for s in snapshots),
                'stage1_tokens':tokens([a.usage for a in articles]),'stage2_tokens':tokens([v.usage for v in versions]),
                'baseline_stage2_tokens':tokens([p['usage'] for p in sample['packages']]),
                'elapsed_seconds':round(time.monotonic()-started,3),
                'snapshots':[{'id':s.id,'state':s.state,'result':s.result,'error':s.error} for s in snapshots],
                'stage3_usage':[{'usage':(s.result or {}).get('usage'),'expansion_usage':(s.result or {}).get('expansion_usage'),
                    'failed_usage':s.quotes.get('failed_usage'),'completed_usage':s.quotes.get('completed_usage')} for s in snapshots],
                'grouping':[{'news_id':a.news_id,'state':a.state,'error':a.error,'usage':a.usage,'result':a.result} for a in articles],
                'package_results':[{'id':p.id,'title':p.title,'fact':p.fact,'revision':p.revision} for p in packages],
                'event_versions':[{'event_id':v.event_id,'state':v.state,'input':v.input,'result':v.result,'usage':v.usage} for v in versions],
                'members':[{'event_id':m.event_id,'package_id':m.package_id} for m in await session.scalars(select(EventMember))],
                'results':[{'id':e.id,'state':e.state,'result':e.result,'error':e.error} for e in events],
                'usage_note':'仅统计接口实际返回的usage；中断请求可能缺少用量，不等于供应商账单。',
                'note':'质量需逐项检查事实边界、归属、证据和时间线后确认；旧基线仅含包研判费用，不代表旧三阶段总价。'}
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)); output.chmod(0o600)
            settings=await session.get(RiskSettings,1); settings.enabled=False; await session.commit()
            print(json.dumps({k:report[k] for k in ('originals','packages','events','failed','unassigned')},ensure_ascii=False))
    finally:
        async with SessionLocal() as session:
            settings=await session.get(RiskSettings,1)
            if settings and ids and settings.provider_id==ids.get('grouping'):
                settings.enabled=False; await session.commit()
        await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--news-ids',type=lambda value: [int(i) for i in value.split(',')],help='Run only these original IDs from the fixed baseline')
    parser.add_argument('--supplement',type=Path,help='Frozen additional real originals; the original baseline stays unchanged')
    args=parser.parse_args()
    asyncio.run(replay(args.baseline,args.credentials,args.output,args.news_ids,args.supplement))
