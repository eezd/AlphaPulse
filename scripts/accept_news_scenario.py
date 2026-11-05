"""Fixed chronological acceptance; credentials and writes stay in disposable CI."""
import asyncio
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from sqlalchemy import func, select
from alphapulse.ai import llm
from alphapulse.ai.skills import create_node, add_version
from alphapulse.core.crypto import encrypt, decrypt
from alphapulse.core.db import SessionLocal, engine
from alphapulse.models import AiProvider, NewsItem, SkillNode, SkillVersion
from alphapulse.news_research import service
from alphapulse.news_research.contracts import INDEX_SKILL, ASSIGN_SKILL, RESEARCH_SKILL
from alphapulse.news_research.models import Package, Attempt, Event, Version, Membership, Reference, VocabularyVersion
from accept_news_research import Budget

MODELS = (Package, Attempt, Event, Version, Membership, Reference, VocabularyVersion)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def save(path, data):
    temporary = path.with_suffix('.pending')
    with open(temporary, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as file:
        json.dump(data, file, ensure_ascii=False, indent=2, default=str)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def validate_manifest(manifest):
    ids = [n['id'] for n in manifest['news']]
    batches = [i for batch in manifest['batches'] for i in batch]
    if len(ids) != 12 or len(set(ids)) != 12 or sorted(ids) != sorted(batches):
        raise ValueError('场景必须包含12条唯一新闻，且每条恰好出现在一个批次')
    by_id = {n['id']: n for n in manifest['news']}
    if len({urlparse(s['url']).hostname for s in manifest.get('official_sources', [])}) < 2:
        raise ValueError('必须冻结两个独立官方域名')
    previous = None
    for batch in manifest['batches']:
        times = [datetime.fromisoformat(by_id[i]['published_at']) for i in batch]
        if not times or (previous and min(times) < previous):
            raise ValueError('批次必须按报道时间排序')
        previous = max(times)
    if manifest['baseline_requests'] != 199 or manifest['additional_limit'] != 120:
        raise ValueError('预算必须保留历史199次并追加最多120次')


async def snapshot():
    async with SessionLocal() as session:
        return {m.__tablename__: [{c.name: getattr(row, c.name) for c in m.__table__.columns}
                for row in await session.scalars(select(m).order_by(*m.__table__.primary_key.columns))]
                for m in MODELS}


async def freeze_config(config):
    async with SessionLocal() as session:
        return {stage: (await service.model_config(session, config, stage))[2]
                for stage in ('index', 'research')}


async def initialize(credentials):
    config = {'concurrency': 3}
    async with SessionLocal() as session:
        if await session.scalar(select(func.count()).select_from(NewsItem)) or await session.scalar(select(func.count()).select_from(SkillNode)):
            raise ValueError('必须使用空的独立CI数据库')
        for stage, content in [('index', INDEX_SKILL + '\n' + ASSIGN_SKILL), ('research', RESEARCH_SKILL)]:
            fields = credentials[stage]
            provider = AiProvider(name='scenario-' + stage, protocol=fields['protocol'], base_url=fields['base_url'],
                api_key_encrypted=encrypt(fields['api_key']), api_key_hint='isolated', model=fields['model'],
                max_tokens=fields['max_tokens'], context_window=fields['context_window'])
            session.add(provider)
            await session.flush()
            skill = await create_node(session, None, 'skill', 'scenario-' + stage, scope='news')
            version = await add_version(session, skill, content, 'user', 'Frozen isolated acceptance')
            skill.published_version_id = version.id
            config[stage] = {'provider_id': provider.id, 'model': 'gpt-6.1-terra',
                'reasoning_effort': 'medium' if stage == 'index' else 'xhigh', 'skill_id': skill.id}
        session.add(VocabularyVersion(revision=1, entries=[{'canonical': '币安', 'aliases': ['Binance']}]))
        await session.commit()
    return config


def assertions(manifest, tables, checkpoints):
    packages = tables['news_research_packages']
    members = tables['news_research_memberships']
    versions = {v['id']: v for v in tables['news_research_versions']}
    links = {n['id']: {m['event_id'] for m in members for p in packages if m['package_id'] == p['id'] and p['news_id'] == n['id']}
             for n in manifest['news']}
    checks = []
    for group in manifest['must_link']:
        checks.append({'name': 'same_event', 'news_ids': group, 'passed': bool(set.intersection(*(links[i] for i in group)))})
    for a, b in manifest['must_separate']:
        checks.append({'name': 'different_events', 'news_ids': [a, b], 'passed': bool(links[a] and links[b]) and not bool(links[a] & links[b])})
    main = links[manifest['must_link'][0][0]]
    natural = {v['id']: v for c in checkpoints for v in c['versions'] if v['event_id'] in main and v['state'] == 'done'}
    revisions = {v['revision'] for v in natural.values()}
    checks.append({'name': 'three_natural_revisions', 'passed': len(revisions) >= 3})
    latest = max(natural.values(), key=lambda v: v['id'], default={})
    covered = {p['news_id'] for p in latest.get('input', {}).get('packages', [])}
    checks.append({'name': 'latest_natural_version_covers_all_main_members', 'passed': set(manifest['must_link'][0]).issubset(covered)})
    for consumer, target in manifest['references']:
        independent = links[consumer] - links[target]
        refs = [r for r in tables['news_research_references'] if versions[r['version_id']]['event_id'] in independent
                and versions[r['target_version_id']]['event_id'] in links[target]]
        event_rows = {e['id']: e for e in tables['news_research_events']}
        pinned = [r for r in refs if versions[r['version_id']]['state'] == 'done'
                  and versions[r['target_version_id']]['state'] == 'done' and any(
                      v['event_id'] == versions[r['target_version_id']]['event_id']
                      and v['revision'] > versions[r['target_version_id']]['revision']
                      and v['id'] > r['version_id'] for v in natural.values())]
        stable = bool(pinned) and all(
            event_rows.get(versions[r['target_version_id']]['event_id'], {}).get('revision', 0)
            > versions[r['target_version_id']]['revision']
            and not any(v['event_id'] == versions[r['version_id']]['event_id']
                        and v['id'] > r['version_id'] for v in versions.values())
            for r in pinned)
        checks.append({'name': 'pinned_reference_without_cascade', 'passed': stable})
    checks.append({'name': 'all_completed', 'passed': len(packages) == 12 and all(p['state'] == 'done' for p in packages)
                   and all(e['state'] == 'done' for e in tables['news_research_events'])})
    return checks


def official_assertions(manifest, tables, external):
    packages = {p['id']: p['news_id'] for p in tables['news_research_packages']}
    links = {n: {m['event_id'] for m in tables['news_research_memberships']
                 if packages[m['package_id']] == n} for n in manifest['external_news_ids']}
    checks = []
    for news_id, source in zip(manifest['external_news_ids'], manifest['official_sources'], strict=True):
        valid = False
        for version in tables['news_research_versions']:
            if version['state'] != 'done' or version['event_id'] not in links[news_id] or version['event_id'] not in external:
                continue
            output = version.get('output') or {}
            refs = [ref for claim in output.get('claims', []) if claim.get('verdict') in ('supported', 'partial')
                    for ref in claim.get('evidence_refs', [])]
            for record in version['evidence']:
                body = record.get('result', {})
                if record['tool'] != 'read_web' or body.get('error') or not body.get('content'):
                    continue
                if urlparse(body.get('url', '')).hostname != urlparse(source['url']).hostname:
                    continue
                if not all(a.casefold() in body['content'].casefold() for a in source['anchors']):
                    continue
                if any(ref['article_id'] == body.get('article_id') and ref['quote'].strip()
                       and ref['quote'] in body['content'] for ref in refs):
                    valid = True
        checks.append({'name': 'official_claim_evidence', 'news_id': news_id,
                       'domain': urlparse(source['url']).hostname, 'passed': valid})
    return checks


def official_hints(manifest, news_ids):
    return '\n验收提供的公开来源线索（链接不是证据）：' + '\n'.join(
        source['url'] for news_id, source in zip(manifest['external_news_ids'], manifest['official_sources'], strict=True)
        if news_id in news_ids)


async def reference_api_assertion(manifest, tables):
    from alphapulse.news_research.api import version_references
    packages = {p['id']: p['news_id'] for p in tables['news_research_packages']}
    versions = {v['id']: v for v in tables['news_research_versions']}
    checks = []
    async with SessionLocal() as session:
        for consumer, target in manifest['references']:
            consumers = {m['event_id'] for m in tables['news_research_memberships'] if packages[m['package_id']] == consumer}
            targets = {m['event_id'] for m in tables['news_research_memberships'] if packages[m['package_id']] == target}
            refs = [r for r in tables['news_research_references']
                    if versions[r['version_id']]['event_id'] in consumers - targets
                    and versions[r['target_version_id']]['event_id'] in targets]
            passed = False
            for ref in refs:
                actual = await version_references(session, ref['version_id'])
                passed |= any(r['version_id'] == ref['target_version_id'] and r['changed'] for r in actual)
            checks.append({'name': 'reference_api_changed_and_pinned', 'passed': passed})
    return checks


async def _run(args):
    limit = request_limit(args)
    url = urlparse(os.environ['DATABASE_URL'])
    if (url.username, url.password, url.path) != ('ci', 'ci-only', '/ci') or url.hostname not in ('localhost', '127.0.0.1'):
        raise ValueError('只能写入独立本地CI数据库')
    manifest = json.loads(args.scenario_manifest.read_text())
    validate_manifest(manifest)
    baseline = Path(manifest['baseline_counter'])
    if int(baseline.read_text()) != 199 or hashlib.sha256(baseline.read_bytes()).hexdigest() != manifest['baseline_counter_sha256']:
        raise ValueError('历史计数不匹配')
    # Preflight precedes all paid calls, using the application's public reader.
    from alphapulse.news_research.tools import fetch_public
    preflight = []
    for source in manifest['official_sources']:
        result = await fetch_public(source['url'])
        if not all(anchor.lower() in result['content'].lower() for anchor in source['anchors']):
            raise ValueError('官方正文缺少预期事实锚点')
        preflight.append({**result, 'sha256': digest(result['content'])})
    credentials = json.loads(args.credentials.read_text())
    if credentials.get('searxng_url'):
        from alphapulse.core.config import get_settings
        get_settings().searxng_url = credentials['searxng_url']
    state_path = args.output / 'state.json'
    if args.resume:
        state = json.loads(state_path.read_text())
        if state['manifest_sha256'] != digest(manifest) or state['credentials_sha256'] != digest(credentials):
            raise ValueError('冻结输入或配置已变化，不能续跑')
        if not args.budget.exists() or int(args.budget.read_text()) < state['requests']:
            raise ValueError('续跑计数缺失或回退')
        if int(args.budget.read_text()) > limit:
            raise ValueError('请求账本已超过所选累计上限，请显式提供已授权上限')
        if await freeze_config(state['config']) != state['rules']:
            raise ValueError('已发布Skill或模型配置已变化')
    else:
        if args.output.exists() or args.budget.exists():
            raise ValueError('场景目录或计数已存在，请使用--resume')
        args.output.mkdir(mode=0o700)
        config = await initialize(credentials)
        state = {'manifest_sha256': digest(manifest), 'credentials_sha256': digest(credentials), 'config': config,
                 'rules': await freeze_config(config), 'batch': 0, 'checkpoints': [], 'requests': 0, 'external': []}
        args.budget.write_text('0'); args.budget.chmod(0o600)
        save(state_path, state)
    save(args.output / 'preflight.json', preflight)
    async with SessionLocal() as session:
        for stage in ('index', 'research'):
            provider = await session.get(AiProvider, state['config'][stage]['provider_id'])
            actual = {k: getattr(provider, k) for k in ('protocol', 'base_url', 'model', 'max_tokens', 'context_window')}
            actual['api_key'] = decrypt(provider.api_key_encrypted)
            if actual != credentials[stage]:
                raise ValueError('隔离模型连接配置与冻结配置不一致')
        vocabulary = list(await session.scalars(select(VocabularyVersion)))
        if len(vocabulary) != 1 or vocabulary[0].revision != 1 or vocabulary[0].entries != [{'canonical': '币安', 'aliases': ['Binance']}]:
            raise ValueError('冻结词库已变化')
        allowed = {i for batch in manifest['batches'][:state['batch'] + 1] for i in batch}
        originals = {n['id']: n for n in manifest['news']}
        for row in await session.scalars(select(NewsItem)):
            if row.id not in allowed:
                raise ValueError('隔离库含未来批次或无关原文')
            actual = {c.name: getattr(row, c.name) for c in NewsItem.__table__.columns}
            if digest(actual) != digest(originals[row.id]):
                raise ValueError('隔离原文与冻结清单不一致')
    if args.resume:
        async with SessionLocal() as session:
            for attempt in await session.scalars(select(Attempt).where(Attempt.state == 'processing')):
                attempt.state = 'failed'
                attempt.error = 'Isolated acceptance interrupted before checkpoint; retry preserves successful stages'
                package = await session.get(Package, attempt.package_id)
                package.state = attempt.stage + '_failed'
            for version in await session.scalars(select(Version).where(Version.state == 'processing')):
                version.state = 'failed'
                version.error = 'Isolated acceptance interrupted before checkpoint'
                event = await session.get(Event, version.event_id)
                event.state = 'failed'
            await session.commit()
    config = state['config']
    # An exclusive run lock prevents concurrent resumes from scheduling duplicate work.
    import fcntl
    with (args.output / '.run.lock').open('w') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        state['request_limit'] = limit
        save(state_path, state)
        budget = Budget(args.budget, limit)
        original_tool = service.Research.tool
        async def chronological_tool(self, name, arguments):
            if name in ('search_web', 'read_web'):
                result = {'status': 'unavailable', 'error': '历史顺序回放不读取当前互联网，最终单独核验'}
                self.records.append({'tool': name, 'arguments': arguments, 'result': result})
                return result
            return await original_tool(self, name, arguments)
        async def process(identifier):
            await service.process_package(identifier, config)
        async def research_pending():
            async with SessionLocal() as session:
                pending = list(await session.scalars(select(Event.id).where(Event.state == 'pending').order_by(Event.id)))
            for identifier in pending:
                await service.process_event(identifier, config, force=True)
        by_id = {n['id']: n for n in manifest['news']}
        with ExitStack() as stack:
            for name in ('_stream_openai', '_stream_anthropic'):
                stack.enter_context(patch.object(llm, name, budget.wrap(getattr(llm, name))))
            with patch.object(service.Research, 'tool', chronological_tool):
                if args.repair_references:
                    if state['batch'] >= len(manifest['batches']):
                        raise ValueError('引用修复须在最终自然更新批次之前执行')
                    save(args.output / f'before-reference-repair-{int(args.budget.read_text())}.json', await snapshot())
                    async with SessionLocal() as session:
                        node = await session.get(SkillNode, config['research']['skill_id'])
                        version = await add_version(session, node, RESEARCH_SKILL, 'user', 'Repair missing explicit incident reference')
                        node.published_version_id = version.id
                        consumers = list(await session.scalars(select(Membership.event_id).join(Package, Package.id == Membership.package_id).where(Package.news_id == manifest['references'][0][0])))
                        main_ids = set(await session.scalars(select(Membership.event_id).join(Package, Package.id == Membership.package_id).where(Package.news_id == manifest['references'][0][1])))
                        for identifier in consumers:
                            if identifier not in main_ids:
                                event = await session.get(Event, identifier)
                                event.revision += 1
                                event.state = 'pending'
                        await session.commit()
                    state['rules'] = await freeze_config(config)
                    save(state_path, state)
                    for identifier in consumers:
                        if identifier not in main_ids:
                            await service.process_event(identifier, config, force=True)
                    state['requests'] = int(args.budget.read_text())
                    save(state_path, state)
                while state['batch'] < len(manifest['batches']) and int(args.budget.read_text()) < limit:
                    batch = manifest['batches'][state['batch']]
                    async with SessionLocal() as session:
                        for identifier in batch:
                            existing = await session.get(NewsItem, identifier)
                            if existing:
                                if existing.content_hash != by_id[identifier]['content_hash']:
                                    raise ValueError('已导入原文发生变化')
                            else:
                                values = {k: v for k, v in by_id[identifier].items() if k in NewsItem.__table__.columns}
                                for k, v in list(values.items()):
                                    if k.endswith('_at') and v:
                                        values[k] = datetime.fromisoformat(v)
                                session.add(NewsItem(**values))
                        await session.commit()
                        await service.enroll(session, batch)
                        packages = list(await session.scalars(select(Package).where(Package.news_id.in_(batch)).order_by(Package.id)))
                        for package in packages:
                            if package.state.endswith('_failed'):
                                latest = await session.scalar(select(Attempt).where(Attempt.package_id == package.id).order_by(Attempt.id.desc()).limit(1))
                                package.state = latest.stage + '_pending'
                        for event in await session.scalars(select(Event).where(Event.state == 'failed')):
                            event.state = 'pending'
                        await session.commit()
                        identifiers = [p.id for p in packages]
                    for offset in range(0, len(identifiers), 3):
                        await asyncio.gather(*(process(i) for i in identifiers[offset:offset + 3]))
                    for identifier in identifiers:
                        await process(identifier)
                    await research_pending()
                    tables = await snapshot()
                    checkpoint = {'batch': batch, 'versions': tables['news_research_versions']}
                    state['requests'] = int(args.budget.read_text())
                    complete = all(p['state'] == 'done' for p in tables['news_research_packages']) and all(e['state'] == 'done' for e in tables['news_research_events'])
                    if complete:
                        state['checkpoints'].append(checkpoint)
                        state['batch'] += 1
                    save(state_path, state)
                    save(args.output / f'attempt-{len(list(args.output.glob("attempt-*.json"))):03}.json', tables)
                    print(json.dumps({'batch': state['batch'], 'requests': state['requests'], 'complete': complete}), flush=True)
                    if not complete:
                        break
            if state['batch'] == len(manifest['batches']) and int(args.budget.read_text()) < limit:
                async with SessionLocal() as session:
                    targets = list(await session.scalars(select(Membership.event_id).join(Package, Package.id == Membership.package_id).where(Package.news_id.in_(manifest['external_news_ids'])).distinct().order_by(Membership.event_id)))
                for identifier in targets:
                    if identifier in state['external'] or int(args.budget.read_text()) >= limit:
                        continue
                    async with SessionLocal() as session:
                        # Supply only this event's official leads, never unrelated sources.
                        news_ids = set(await session.scalars(select(Package.news_id).join(Membership).where(Membership.event_id == identifier)))
                        content = RESEARCH_SKILL + official_hints(manifest, news_ids)
                        node = await session.get(SkillNode, config['research']['skill_id'])
                        version = await session.scalar(select(SkillVersion).where(SkillVersion.skill_id == node.id, SkillVersion.content == content).order_by(SkillVersion.version_no.desc()).limit(1))
                        if version is None:
                            version = await add_version(session, node, content, 'user', 'Event-specific official leads for final live verification')
                        node.published_version_id = version.id
                        event = await session.get(Event, identifier)
                        if event.state == 'done':
                            event.revision += 1
                        event.state = 'pending'
                        await session.commit()
                    state['rules'] = await freeze_config(config)
                    save(state_path, state)
                    await service.process_event(identifier, config, force=True)
                    async with SessionLocal() as session:
                        event = await session.get(Event, identifier)
                        if event.state == 'done':
                            state['external'].append(identifier)
                    state['requests'] = int(args.budget.read_text())
                    save(state_path, state)
        tables = await snapshot()
        checks = assertions(manifest, tables, state['checkpoints'])
        checks.extend(await reference_api_assertion(manifest, tables))
        checks.extend(official_assertions(manifest, tables, state['external']))
        report = {'quality_accepted': False, 'long_running_quality_verified': False, 'automated_passed': all(c['passed'] for c in checks),
                  'assertions': checks, 'requests_new': int(args.budget.read_text()), 'requests_historical': 199,
                  'requests_cumulative': 199 + int(args.budget.read_text()), 'new_cap': limit, 'manifest': manifest,
                  'rules': state['rules'], 'checkpoints': state['checkpoints'], 'tables': tables,
                  'usage_note': '缺失usage不等于零；URL由验收提供；历史回放关闭外网；人工审阅后仅限定范围放行。'}
        save(args.output / f'report-{len(list(args.output.glob("report-*.json"))):03}.json', report)
    await engine.dispose()


def request_limit(args):
    limit = getattr(args, 'request_limit', None)
    if limit is None:
        return 120 if args.finish else 90
    if not args.resume or not args.finish or limit < 120:
        raise ValueError('追加上限仅用于--resume --finish，且不能低于原120次')
    return limit


async def run(args):
    import fcntl
    # Hold the lease before reading checkpoints or changing any run files.
    lock_path = args.output.with_name(args.output.name + '.lock')
    with open(lock_path, 'a', opener=lambda p, f: os.open(p, f, 0o600)) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            await _run(args)
        finally:
            await engine.dispose()
