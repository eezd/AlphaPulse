"""新分析/报表的持久化 claim 与跨进程瞬态进度投影。"""
import asyncio
from datetime import UTC, datetime
import logging
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, field_validator
from redis.exceptions import RedisError
from sqlalchemy import select
from alphapulse.models import NewsItem
from alphapulse.briefings.contracts import StrictModel
from alphapulse.briefings.models import Analysis, Report, ReportPart
from alphapulse.ai import progress

logger = logging.getLogger(__name__)


class WaitTarget(StrictModel):
    task_id: UUID
    wait_id: UUID


class ResetWaits(StrictModel):
    targets: list[WaitTarget] = Field(min_length=1)

    @field_validator('targets')
    @classmethod
    def unique_targets(cls, targets):
        result = list({(t.task_id, t.wait_id): t for t in targets}.values())
        if len(result) > 16:
            raise ValueError('一次最多立即重试 16 个等待')
        return result


async def queue_view(session, *, strict=False):
    analyses = list(await session.scalars(select(Analysis).where(Analysis.state == 'processing')))
    reports = list(await session.scalars(select(Report).where(Report.state == 'processing')))
    parts = list(await session.scalars(select(ReportPart).where(ReportPart.state == 'processing')))
    parts_by_report = {part.report_id: part for part in parts}
    ids = {row.news_id for row in analyses}
    news = {row.id: row for row in (await session.execute(select(NewsItem.id, NewsItem.title, NewsItem.url,
        NewsItem.source_code, NewsItem.published_at).where(NewsItem.id.in_(ids)))).all()}
    status, snapshot = await progress.read_snapshot(strict=strict)
    records = {(record['claim']['kind'], record['claim']['id']): record for record in snapshot['tasks']
               if record.get('claim')} if status == 'available' else {}
    items = []
    entries = [(row, 'analysis', row.id, None) for row in analyses]
    entries += [(parts_by_report.get(row.id, row), 'report_part' if row.id in parts_by_report else 'report', row.id, row)
                for row in reports]
    for row, kind, object_id, parent in entries:
        stage = 'report' if parent else 'analysis'
        record = records.get((kind, row.id))
        if record and (record['stage'] != stage or record['object_id'] != object_id):
            record = None
        sources = []
        if kind == 'analysis':
            frozen = row.input or {}
            item = news.get(row.news_id)
            sources.append({'id': row.news_id, 'package_id': None,
                'title': frozen.get('title') or (item.title if item else '原文不可用'),
                'source_code': frozen.get('source_code') or (item.source_code if item else '来源不可用'),
                'published_at': frozen.get('published_at') or (item.published_at if item else None),
                'url': frozen.get('url') or (item.url if item else '')})
        details = {key: record.get(key) for key in ('phase', 'operation', 'updated_at', 'model', 'reasoning_effort',
            'round', 'tool', 'current_news_id', 'retry_attempt', 'retry_at', 'wait_id', 'part_key')} if record else None
        items.append({'claim': {'kind': kind, 'id': row.id}, 'stage': stage,
            'task_id': record['task_id'] if record else None, 'package_id': None, 'event': None,
            'report': {'id': parent.id, 'kind': parent.kind, 'report_date': parent.report_date,
                       'version_no': parent.version_no} if parent else None,
            'analysis_id': row.id if kind == 'analysis' else None,
            'news': sources, 'started_at': row.started_at or row.created_at, 'progress': details,
            'can_retry_now': bool(record and record.get('wait_id') and record.get('retry_at'))})
    items.sort(key=lambda item: (item['started_at'].isoformat(), item['claim']['kind'], item['claim']['id']))
    return {'checked_at': datetime.now(UTC).isoformat(), 'progress_status': status,
            'worker_heartbeat_at': snapshot['heartbeat_at'] if snapshot else None, 'items': items}


async def reset_waits(session, body: ResetWaits):
    try:
        view = await queue_view(session, strict=True)
    except (RedisError, TimeoutError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(503, '立即重试请求结果未确认，请刷新队列后查看') from error
    live = {(i['task_id'], i['progress']['wait_id']) for i in view['items'] if i['can_retry_now']}
    result = {'scheduled': [], 'already_requested': [], 'skipped': []}
    targets = []
    for target in body.targets:
        value = {'task_id': target.task_id.hex, 'wait_id': target.wait_id.hex}
        if (value['task_id'], value['wait_id']) in live:
            targets.append(value)
        else:
            result['skipped'].append({**value, 'reason': 'stale' if view['progress_status'] != 'available' else 'not_waiting'})
    if not targets:
        return result
    try:
        async with asyncio.timeout(progress.IO_TIMEOUT), progress.redis.pipeline(transaction=True) as pipe:
            for target in targets:
                pipe.set(progress.WAKE_PREFIX + target['wait_id'], target['task_id'], ex=90, nx=True)
            responses = await pipe.execute()
        for target, response in zip(targets, responses, strict=True):
            result['scheduled' if response else 'already_requested'].append(target)
    except (RedisError, TimeoutError) as error:
        logger.warning('立即重试命令登记结果未确认', exc_info=True)
        raise HTTPException(503, '立即重试请求结果未确认，请刷新队列后查看') from error
    return result
