"""Restore a frozen acceptance checkpoint into an empty local CI database only.

No network tools or model calls run here. Historical attempts retain their rules;
new work uses explicitly supplied providers and newly frozen isolated Skills.
"""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import DateTime, func, select, text
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import NewsItem
from accept_news_scenario import MODELS, digest, freeze_config, initialize, save, validate_manifest


def prepare(report, tables):
    manifest = report['manifest']
    validate_manifest(manifest)
    ids = {p['news_id'] for p in tables['news_research_packages']}
    imported = set()
    batch_index = None
    for index, batch in enumerate(manifest['batches'], 1):
        imported.update(batch)
        if imported == ids:
            batch_index = index
            break
    if batch_index is None or any(p['state'] != 'done' for p in tables['news_research_packages']):
        raise ValueError('快照不是完整的已完成时间批次')
    if any(e['state'] not in ('done', 'retired') for e in tables['news_research_events']):
        raise ValueError('快照包含未完成事件')
    originals = {n['id']: n for n in manifest['news']}
    if any(p['content_hash'] != originals[p['news_id']]['content_hash'] for p in tables['news_research_packages']):
        raise ValueError('快照原文哈希与冻结样本不一致')
    versions = {v['id']: v for v in tables['news_research_versions']}
    if any(v['state'] == 'processing' for v in versions.values()):
        raise ValueError('不能从进行中的版本恢复')
    known = {v['id']: v for v in report['tables']['news_research_versions']}
    if any(digest(v) != digest(known.get(v['id'])) for v in versions.values()):
        raise ValueError('快照版本与原报告不一致')
    checkpoints = report['checkpoints'][:batch_index]
    if len(checkpoints) != batch_index or {v['id'] for v in checkpoints[-1]['versions']} != set(versions):
        raise ValueError('快照与报告时间检查点不一致')
    if any(n['id'] not in ids for v in versions.values() for n in v['input']['articles']):
        raise ValueError('快照包含未来原文')
    return manifest, [n for n in manifest['news'] if n['id'] in ids], batch_index, checkpoints


def values(model, row):
    result = {c.name: row[c.name] for c in model.__table__.columns if c.name in row}
    for column in model.__table__.columns:
        if isinstance(column.type, DateTime) and isinstance(result.get(column.name), str):
            result[column.name] = datetime.fromisoformat(result[column.name])
    return result


def counter_limit(report, count):
    limit = report.get('new_cap', report['manifest']['additional_limit'])
    if limit < report['manifest']['additional_limit'] or not report['requests_new'] <= count <= limit:
        raise ValueError('必须沿用未回退的原始请求账本及报告累计上限')
    return limit


async def restore(args):
    url = urlparse(os.environ['DATABASE_URL'])
    if (url.username, url.password, url.path) != ('ci', 'ci-only', '/ci') or url.hostname not in ('localhost', '127.0.0.1'):
        raise ValueError('只能恢复到空的本地CI数据库')
    report = json.loads(args.report.read_text())
    tables = json.loads(args.snapshot.read_text())
    manifest, originals, batch, checkpoints = prepare(report, tables)
    if args.output.exists():
        raise ValueError('恢复目录必须是新目录，禁止覆盖旧报告')
    count = int(args.budget.read_text())
    limit = counter_limit(report, count)
    baseline = Path(manifest['baseline_counter'])
    if int(baseline.read_text()) != manifest['baseline_requests'] or hashlib.sha256(baseline.read_bytes()).hexdigest() != manifest['baseline_counter_sha256']:
        raise ValueError('历史账本已变化')
    async with SessionLocal() as session:
        for model in (NewsItem, *MODELS):
            if await session.scalar(select(func.count()).select_from(model)):
                raise ValueError('恢复数据库必须为空')
    credentials = json.loads(args.credentials.read_text())
    config = await initialize(credentials)
    async with SessionLocal() as session:
        await session.execute(NewsItem.__table__.insert(), [values(NewsItem, row) for row in originals])
        # Foreign keys require parents to be restored first; vocabulary was frozen by initialize.
        from alphapulse.news_research.models import Package, Event, Membership, Attempt, Version, Reference, VocabularyVersion
        # Compare content/revision, since the new row has a different creation time.
        old = [(r['revision'], r['entries']) for r in tables[VocabularyVersion.__tablename__]]
        new = [(r['revision'], r['entries']) for r in await _vocabulary(session)]
        if old != new:
            raise ValueError('恢复词库与冻结词库不一致')
        for model in (Package, Event, Membership, Attempt, Version, Reference):
            rows = tables[model.__tablename__]
            if rows:
                await session.execute(model.__table__.insert(), [values(model, row) for row in rows])
        for model in (NewsItem, Package, Event, Attempt, Version, Reference):
            # Table identifiers are fixed model metadata, never user input.
            name = model.__tablename__
            await session.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {name}"))
        await session.commit()
    args.output.mkdir(mode=0o700)
    state = {'manifest_sha256': digest(manifest), 'credentials_sha256': digest(credentials), 'config': config,
             'rules': await freeze_config(config), 'batch': batch, 'checkpoints': checkpoints,
             'requests': count, 'request_limit': limit, 'external': [], 'restored_from': {
                 'report_sha256': hashlib.sha256(args.report.read_bytes()).hexdigest(),
                 'snapshot_sha256': hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
                 'historical_rules_preserved': True, 'new_rules_require_model_revalidation': True}}
    save(args.output / 'state.json', state)
    print(json.dumps({'restored_news': len(originals), 'next_batch': batch, 'requests_new': count, 'model_calls': 0}))


async def _vocabulary(session):
    from alphapulse.news_research.models import VocabularyVersion
    return [{'revision': v.revision, 'entries': v.entries} for v in await session.scalars(select(VocabularyVersion))]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('report', 'snapshot', 'credentials', 'output', 'budget'):
        parser.add_argument('--' + name, type=Path, required=True)
    async def main():
        try:
            await restore(parser.parse_args())
        finally:
            await engine.dispose()
    asyncio.run(main())
