"""Export the accepted twenty-package baseline through a read-only DB transaction.

Run inside the API environment; stdout contains news and AI results, never provider
credentials. This exports inputs only and cannot start workers or call models.
"""
import asyncio
import json
from datetime import UTC, datetime
from sqlalchemy import select, text
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from alphapulse.models.risk import ArticleAnalysis, NewsSummary, SummaryArticle, SummaryVersion, PackageEvidence, RiskSettings

SAMPLE_IDS = [126, 140, 763, 774, 781, 769, 765, 94, 96, 97, 776, 623, 7, 2, 227, 88, 768, 770, 762, 761]


async def export():
    start = datetime.fromisoformat('2026-11-01T00:00:00+08:00')
    end = datetime.fromisoformat('2026-11-03T00:00:00+08:00')
    packages, news = [], {}
    try:
        async with SessionLocal() as session:
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            settings = await session.get(RiskSettings, 1)
            if settings.enabled:
                raise ValueError('请先暂停线上AI再导出固定样本')
            for identifier in SAMPLE_IDS:
                package = await session.get(NewsSummary, identifier)
                if package is None or package.replacement_ids or package.state != 'done':
                    raise ValueError(f'样本包#{identifier}已变化，请重新审核样本')
                version = await session.scalar(select(SummaryVersion).where(SummaryVersion.summary_id == identifier,
                    SummaryVersion.revision == package.published_revision, SummaryVersion.state == 'done'))
                if version is None:
                    raise ValueError(f'样本包#{identifier}缺少成功版本')
                rows = await session.scalars(select(NewsItem).join(ArticleAnalysis, ArticleAnalysis.news_id == NewsItem.id)
                    .join(SummaryArticle, SummaryArticle.analysis_id == ArticleAnalysis.id).where(
                        SummaryArticle.summary_id == identifier, SummaryArticle.active.is_(True),
                        ArticleAnalysis.content_hash == NewsItem.content_hash))
                source_ids = []
                for row in rows:
                    if not start <= row.published_at < end:
                        raise ValueError(f'样本新闻#{row.id}超出时间范围')
                    source_ids.append(row.id)
                    news[row.id] = {key: getattr(row, key) for key in ('id', 'source_code', 'url', 'title', 'summary',
                        'content_text', 'content_hash', 'revision_no', 'published_at', 'source_updated_at')}
                if not source_ids:
                    raise ValueError(f'样本包#{identifier}无有效原文')
                records = list(await session.scalars(select(PackageEvidence).where(PackageEvidence.version_id == version.id)
                    .order_by(PackageEvidence.id)))
                packages.append({'id': identifier, 'title': package.title, 'revision': version.revision,
                    'news_ids': sorted(set(source_ids)), 'input': version.input, 'result': version.result,
                    'rules': version.rules, 'model': version.model, 'usage': version.usage,
                    'started_at': version.started_at, 'finished_at': version.finished_at,
                    'research': [r.data for r in records]})
            return {'exported_at': datetime.now(UTC), 'start': start, 'end': end,
                'model_settings': {key: getattr(settings, key) for key in ('model', 'second_model', 'third_model',
                    'reasoning_effort', 'second_reasoning_effort', 'third_reasoning_effort', 'max_concurrency')},
                'packages': packages, 'news': sorted(news.values(), key=lambda n: (n['published_at'], n['id']))}
    finally:
        await engine.dispose()


if __name__ == '__main__':
    print(json.dumps(asyncio.run(export()), ensure_ascii=False, default=str))
