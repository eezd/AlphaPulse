"""Inspect or reset only news AI derivatives after isolated sample acceptance."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from sqlalchemy import delete, func, select, text
from alphapulse.core.db import SessionLocal, engine
from alphapulse.core.redis import redis
from alphapulse.models.risk import (RiskSettings, ReprocessTarget, ReprocessItem, ReprocessJob,
    EventEvidence, EventCorrectionRecord, EventLink, EventVersion, EventMember, NewsEvent,
    PackageCorrection, PackageEvidence, SummaryVersion, GroupingVersion, SummaryArticle,
    NewsSummary, ArticleAnalysis, RiskSnapshot, RiskAnalysis, RiskEvent)
from alphapulse.news_risk.scheduler import GATE

DERIVED = (ReprocessTarget, ReprocessItem, ReprocessJob, EventEvidence, EventCorrectionRecord,
    EventLink, EventVersion, EventMember, NewsEvent, PackageCorrection, PackageEvidence,
    SummaryVersion, GroupingVersion, SummaryArticle, NewsSummary, ArticleAnalysis,
    RiskSnapshot, RiskAnalysis, RiskEvent)


def accepted(report, baseline):
    return (report.get('real_models') is True and report.get('quality_accepted') is True
        and report.get('baseline_sha256') == hashlib.sha256(baseline).hexdigest()
        and report.get('baseline_packages') == 20 and report.get('originals') == 27
        and report.get('failed') == 0 and report.get('unassigned') == 0)


async def reset(execute=False, report=None, baseline=None):
    if execute and (not report or not baseline or not accepted(report,baseline)):
        raise ValueError('缺少通过质量验收的固定20包真实模型报告；禁止清理')
    try:
        async with engine.connect() as gate:
            if not await gate.scalar(text('SELECT pg_try_advisory_lock(:gate,1)'),{'gate':GATE}):
                raise ValueError('仍有AI任务运行，不能清理')
            try:
                async with SessionLocal() as session:
                    settings=await session.get(RiskSettings,1,with_for_update=True)
                    if settings and settings.enabled: raise ValueError('必须先暂停新闻AI')
                    counts={model.__tablename__:await session.scalar(select(func.count()).select_from(model)) for model in DERIVED}
                    if execute:
                        for model in DERIVED: await session.execute(delete(model))
                        await session.commit()
                        async for key in redis.scan_iter(match='ap:risk:*',count=100): await redis.delete(key)
                    return {'executed':execute,'counts':counts}
            finally:
                await gate.execute(text('SELECT pg_advisory_unlock(:gate,1)'),{'gate':GATE})
                await gate.commit()
    finally:
        await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--acceptance',type=Path)
    parser.add_argument('--baseline',type=Path)
    args=parser.parse_args()
    print(json.dumps(asyncio.run(reset(args.execute,
        json.loads(args.acceptance.read_text()) if args.acceptance else None,
        args.baseline.read_bytes() if args.baseline else None)),ensure_ascii=False))
