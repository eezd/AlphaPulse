"""Queue presentation combines durable work with short-lived execution steps."""
import json
from datetime import UTC, datetime
from sqlalchemy import select, func, or_, exists
from alphapulse.models import NewsItem
from alphapulse.models.risk import (ReprocessItem, ReprocessTarget, ArticleAnalysis, NewsSummary, NewsEvent, RiskSnapshot, RiskSettings, ReprocessJob)
from alphapulse.core.redis import redis


async def job_out(session, job):
    counts = dict((await session.execute(select(ReprocessItem.state,func.count()).where(ReprocessItem.job_id==job.id).group_by(ReprocessItem.state))).all())
    if not job.settings.get('request'):
        counts = {state:sum(i.get('state','pending')==state for i in job.items) for state in ('pending','processing','done','failed')}
        return {'id':job.id,'state':job.state,'total':len(job.items),'completed':job.position,'failures':job.failures,'request':None,'counts':counts}
    stages = {}
    for kind, model in (('analysis',NewsSummary),('events',NewsEvent)):
        stages[kind] = dict((await session.execute(select(model.state,func.count()).join(
            ReprocessTarget,ReprocessTarget.object_id==model.id).where(ReprocessTarget.job_id==job.id,
                ReprocessTarget.kind==kind).group_by(model.state))).all())
    return {'id':job.id,'state':job.state,'request':job.settings['request'],'counts':counts,'stages':stages,
        'total':sum(counts.values()),'completed':sum(counts.get(s,0) for s in ('done','failed','skipped')),'failures':job.failures}


async def listing(session, state, stage, origin, job_id, page, limit):
    now = datetime.now(UTC)
    runtime = await redis.get('ap:risk:concurrency')
    runtime = json.loads(runtime) if runtime else None
    raw = await redis.get('ap:risk:active')
    active = json.loads(raw) if raw else None
    settings = await session.get(RiskSettings,1)
    entries=[]
    total=0
    offset=(page-1)*limit
    for kind,model in [('grouping',ArticleAnalysis),('analysis',NewsSummary),('events',NewsEvent),('outlook',RiskSnapshot),('backfill',ReprocessItem)]:
        if stage and stage!=kind:continue
        query=select(model)
        busy = [a['object_id'] for a in (active or []) if a['stage']==kind]
        if kind=='grouping':
            held = exists(select(ReprocessItem.id).join(ReprocessJob).where(ReprocessItem.news_id==model.news_id,
                ReprocessItem.content_hash==model.content_hash, ReprocessItem.state.in_(['pending','processing']),
                ReprocessJob.state.in_(['pending','running'])))
            running = model.id.in_(busy) if active is not None else (
                (model.state=='processing') & ~held.where(ReprocessItem.state=='processing'))
            query=query.join(NewsItem).where(model.content_hash==NewsItem.content_hash,or_(~held,running))
        pending = model.state.in_(['pending','processing','reprocess'])
        due = getattr(model,'due_at',None)
        waiting = due > now if due is not None else False
        if kind=='events':
            from alphapulse.models.risk import EventMember
            blocked=select(EventMember.event_id).join(NewsSummary,NewsSummary.id==EventMember.package_id).where(
                NewsSummary.replacement_ids==[],NewsSummary.state!='withdrawn',
                or_(NewsSummary.state!='done',NewsSummary.result==None))
            waiting=or_(waiting,model.id.in_(blocked))
        if state=='running': query=query.where(model.id.in_(busy) if active is not None else model.state=='processing')
        elif state=='waiting': query=query.where(pending,model.id.not_in(busy),waiting)
        elif state=='queued': query=query.where(pending,model.state!='processing' if active is None else True,model.id.not_in(busy),~waiting if due is not None else True)
        elif state in ('failed','done'):query=query.where(model.state==state)
        else:query=query.where(pending)
        if kind=='backfill':
            query=query.join(ReprocessJob,ReprocessJob.id==model.job_id).where(or_(ReprocessJob.state.in_(['pending','running']),model.state=='processing') if state not in ('failed','done') else True)
            if origin=='realtime':continue
            if job_id:query=query.where(model.job_id==job_id)
        elif kind in ('analysis','events'):
            batch=model.review_config['job_id'].as_integer()
            if origin:query=query.where(batch.is_not(None) if origin=='history' else batch.is_(None))
            if job_id:
                tracked=exists(select(ReprocessTarget.id).where(ReprocessTarget.job_id==job_id,
                    ReprocessTarget.kind==kind,ReprocessTarget.object_id==model.id))
                query=query.where(or_(tracked,batch==job_id))
        elif kind=='outlook':
            if job_id:query=query.where(model.quotes['job_id'].as_integer()==job_id)
            if origin:query=query.where(model.quotes['job_id'].as_integer().is_not(None) if origin=='history' else model.quotes['job_id'].as_integer().is_(None))
        elif job_id or origin=='history':continue
        count = await session.scalar(select(func.count()).select_from(query.subquery()))
        total += count
        if offset >= count:
            offset -= count; continue
        if len(entries) == limit: continue
        rows=(await session.scalars(query.order_by(model.id.desc()).offset(offset).limit(limit-len(entries)))).all()
        offset=0
        for obj in rows:
            url = None
            if kind=='backfill':
                news=await session.get(NewsItem,obj.news_id); title=news.title if news else f'新闻 #{obj.news_id}'
                batch=obj.job_id
                url=news.url if news else None
            elif kind=='grouping':title=obj.input.get('title','');batch=None;url=obj.input.get('url')
            elif kind=='outlook':title='72小时阅读与趋势';batch=obj.quotes.get('job_id')
            else:title=obj.title;batch=(obj.review_config or {}).get('job_id')
            source='history' if batch else 'realtime'
            config_stage = 'grouping' if kind=='backfill' else 'analysis' if kind=='events' else kind
            frozen = (await session.get(ReprocessJob,batch)).settings if batch else None
            config = frozen['configurations'][config_stage]['provider'] if frozen else {}
            if not config and settings:
                prefix = {'grouping':'','analysis':'second_','outlook':'third_'}[config_stage]
                from alphapulse.models import AiProvider
                provider = await session.get(AiProvider,getattr(settings,prefix+'provider_id')) if getattr(settings,prefix+'provider_id') else None
                config = {'model':getattr(settings,prefix+'model') or (provider.model if provider else None),
                    'reasoning_effort':getattr(settings,prefix+'reasoning_effort') if getattr(settings,prefix+'reasoning_effort') is not None else provider.reasoning_effort if provider else None}
            actual=next((a for a in (active or []) if a['stage']==kind and a['object_id']==obj.id),None)
            waiting = getattr(obj,'due_at',now)>now
            status='running' if actual else 'waiting' if waiting else 'queued' if obj.state in ('pending','processing','reprocess') else obj.state
            if state: status=state
            if obj.state=='processing' and active is None: status='unknown'
            entries.append({'key':f'{kind}:{obj.id}','stage':kind,'object_id':obj.id,'title':title,'job_id':batch,
                'url':url,'finished_at':getattr(obj,'finished_at',None),'origin':source,'state':status,'phase':actual.get('phase') if actual else '已完成' if status=='done' else '处理失败' if status=='failed' else '等待条件或聚合时间' if status=='waiting' else 'Worker心跳过期，执行状态未知' if status=='unknown' else 'AI已暂停' if settings and not settings.enabled else '等待调度',
                'started_at':actual.get('started_at') if actual else getattr(obj,'started_at',None),
                'updated_at':actual.get('updated_at') if actual else None,'round':actual.get('round') if actual else None,
                'tool':actual.get('tool') if actual else None,'model':(actual or {}).get('model') or config.get('model') or getattr(obj,'model',None),
                'reasoning_effort':actual.get('reasoning_effort',config.get('reasoning_effort')) if actual else config.get('reasoning_effort'),'error':getattr(obj,'error',None)})
    order = {'grouping':0,'analysis':1,'events':2,'outlook':3,'backfill':4}
    entries.sort(key=lambda r:(order[r['stage']],-r['object_id']))
    return {'items':entries,'total':total,'as_of':now,'worker_alive':active is not None,'concurrency':runtime,'enabled':bool(settings and settings.enabled),'limit':settings.max_concurrency if settings else 5}
