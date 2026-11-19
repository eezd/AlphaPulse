"""Schema guard and bounded AI drain; never call a model provider or execute migrations."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from dotenv import dotenv_values


BRIEFING_HEAD = 'a18901234567'
RESEARCH_ADMISSION_HEAD = 'd41234567890'
ANALYSIS_PARTS_HEAD = 'e52345678901'
EVIDENCE_OFFSETS_HEAD = 'f63456789012'
RESEARCH_HEAD = 'c30123456789'
WSCN_RETIREMENT_HEAD = 'b29012345678'
LEGACY_HEAD = 'f07890123456'


def schema_compatible(actual, expected, *, previous=False):
    predecessors = {(BRIEFING_HEAD, LEGACY_HEAD), (WSCN_RETIREMENT_HEAD, BRIEFING_HEAD), (RESEARCH_HEAD, WSCN_RETIREMENT_HEAD), (RESEARCH_ADMISSION_HEAD, RESEARCH_HEAD), (ANALYSIS_PARTS_HEAD, RESEARCH_ADMISSION_HEAD), (EVIDENCE_OFFSETS_HEAD, ANALYSIS_PARTS_HEAD)}
    return actual == expected or (previous and len(actual) == len(expected) == 1
                                 and (next(iter(actual)), next(iter(expected))) in predecessors)


def fingerprint(row):
    value = {'config': row['config'], 'start_at': str(row['start_at']), 'realtime_from': str(row['realtime_from'])}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def briefing_fingerprint(row):
    value = {'config': row['config'], 'enabled_at': str(row['enabled_at'])}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.next')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


async def briefing_controls(session, *, lock=False):
    from sqlalchemy import text
    exists = await session.scalar(text("SELECT to_regclass('public.briefing_settings')"))
    if exists is None:
        return None
    statement = 'SELECT enabled, control_revision, config, enabled_at FROM briefing_settings WHERE id=1'
    result = await session.execute(text(statement + (' FOR UPDATE' if lock else '')))
    return result.mappings().one_or_none()


async def legacy_controls(session, *, lock=False):
    """Deployment rollback reads archived tables without retaining their ORM."""
    from sqlalchemy import text
    if await session.scalar(text("SELECT to_regclass('public.news_research_settings')")) is None:
        return None
    statement = 'SELECT enabled, control_revision, config, start_at, realtime_from FROM news_research_settings WHERE id=1'
    return (await session.execute(text(statement + (' FOR UPDATE' if lock else '')))).mappings().one_or_none()


async def new_counts(session):
    from sqlalchemy import text
    if await briefing_controls(session) is None:
        return {}
    result = {}
    for label, table, state in [('analyses', 'briefing_analyses', 'processing'),
                                ('reports', 'briefing_reports', 'processing'),
                                ('report_parts', 'briefing_report_parts', 'processing'),
                                ('notifications', 'briefing_notifications', 'sending')]:
        # Table and state names are fixed program constants, never user input.
        result[label] = await session.scalar(text(f'SELECT count(*) FROM {table} WHERE state=:state'), {'state': state})
    if await session.scalar(text("SELECT to_regclass('public.briefing_analysis_parts')")) is not None:
        result['analysis_parts'] = await session.scalar(text("SELECT count(*) FROM briefing_analysis_parts WHERE state='processing'"))
    return result


async def execute(action, journal, base):
    from sqlalchemy import text
    from alphapulse.core.db import SessionLocal, engine
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    expected = set(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
    supports_briefings = expected in ({BRIEFING_HEAD}, {WSCN_RETIREMENT_HEAD}, {RESEARCH_HEAD}, {RESEARCH_ADMISSION_HEAD}, {ANALYSIS_PARTS_HEAD}, {EVIDENCE_OFFSETS_HEAD})
    try:
        if action in ('schema', 'schema-previous'):
            async with engine.connect() as connection:
                actual = set((await connection.execute(text('SELECT version_num FROM alembic_version'))).scalars())
            if not schema_compatible(actual, expected, previous=action == 'schema-previous'):
                raise RuntimeError(f'Schema mismatch: database={actual}, candidate={expected}; migration requires separate authorization')
            print('Schema verified:', ','.join(sorted(actual)), '(explicit additive predecessor)' if actual != expected else '')
            return
        state = json.loads(journal.read_text())
        if action == 'pause':
            async with SessionLocal() as session:
                row = await legacy_controls(session, lock=True)
                controls = await briefing_controls(session, lock=True)
                if row:
                    state['ai'] = {'enabled': row['enabled'], 'revision': row['control_revision'] + 1, 'fingerprint': fingerprint(row)}
                if controls:
                    state['briefings'] = {'enabled': controls['enabled'], 'revision': controls['control_revision'] + 1,
                                          'fingerprint': briefing_fingerprint(controls)}
                # Journal precedes both DB updates, so interrupted maintenance is recoverable.
                save(journal, state)
                if row:
                    await session.execute(text('UPDATE news_research_settings SET enabled=false, control_revision=control_revision+1 WHERE id=1'))
                if controls:
                    await session.execute(text('UPDATE briefing_settings SET enabled=false, control_revision=control_revision+1 WHERE id=1'))
                await session.commit()
            print('Both AI admissions paused; existing calls may finish', flush=True)
        elif action == 'assert-paused':
            async with SessionLocal() as session:
                row = await legacy_controls(session)
                previous = state.get('ai')
                if previous:
                    if not (row and not row['enabled'] and row['control_revision'] == previous['revision']
                            and fingerprint(row) == previous['fingerprint']):
                        raise RuntimeError('Legacy AI controls changed during maintenance; aborting before service stop')
                elif row:
                    raise RuntimeError('Legacy AI configuration was created during maintenance; aborting service stop')
                controls = await briefing_controls(session)
                saved = state.get('briefings')
                if saved:
                    if not (controls and not controls['enabled'] and controls['control_revision'] == saved['revision']
                            and briefing_fingerprint(controls) == saved['fingerprint']):
                        raise RuntimeError('Briefing AI controls changed during maintenance; aborting before service stop')
                elif controls:
                    raise RuntimeError('Briefing configuration was created during maintenance; aborting service stop')
        elif action in ('restore', 'restore-rollback'):
            rollback_to_legacy = action == 'restore-rollback' and not supports_briefings
            async with SessionLocal() as session:
                row = await legacy_controls(session, lock=True)
                previous = state.get('ai')
                if row and previous and not row['enabled'] and row['control_revision'] == previous['revision'] and fingerprint(row) == previous['fingerprint']:
                    restored = previous['enabled'] if rollback_to_legacy or not supports_briefings else False
                    await session.execute(text('UPDATE news_research_settings SET enabled=:enabled, control_revision=control_revision+1 WHERE id=1'), {'enabled': restored})
                    print('Legacy AI enabled state restored:', restored)
                elif row and supports_briefings and row['enabled']:
                    await session.execute(text('UPDATE news_research_settings SET enabled=false, control_revision=control_revision+1 WHERE id=1'))
                    print('Legacy paid workflow remains retired')
                controls = await briefing_controls(session, lock=True)
                saved = state.get('briefings')
                if controls:
                    if rollback_to_legacy:
                        await session.execute(text('UPDATE briefing_settings SET enabled=false, control_revision=control_revision+1 WHERE id=1'))
                        print('New workflow disabled for legacy rollback')
                    elif saved and not controls['enabled'] and controls['control_revision'] == saved['revision'] and briefing_fingerprint(controls) == saved['fingerprint']:
                        await session.execute(text('UPDATE briefing_settings SET enabled=:enabled, control_revision=control_revision+1 WHERE id=1'), {'enabled': saved['enabled']})
                        print('Briefing AI enabled state restored:', saved['enabled'])
                    else:
                        print('Briefing controls changed during maintenance; preserving newer setting')
                await session.commit()
        elif action in ('drain', 'drain-without-api'):
            import httpx
            from fastapi.security import HTTPAuthorizationCredentials
            from alphapulse.core.auth import create_token, require_session, revoke_session
            from alphapulse.core.config import get_settings
            principal = None
            if action == 'drain':
                token, _ = await create_token(get_settings().admin_username)
                principal = await require_session(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
            try:
                deadline = time.monotonic() + 3600
                async with httpx.AsyncClient(timeout=15) as client:
                    while True:
                        async with SessionLocal() as session:
                            attempts = await session.scalar(text("SELECT count(*) FROM news_research_attempts WHERE state='processing'"))
                            versions = await session.scalar(text("SELECT count(*) FROM news_research_versions WHERE state='processing'"))
                            current = await new_counts(session)
                        chat = False
                        if principal is not None:
                            response = await client.get(base + '/api/chat/session', headers={'Authorization': 'Bearer ' + token})
                            response.raise_for_status()
                            chat = response.json()['data']['generating']
                        print(f'Draining: attempts={attempts}, event_versions={versions}, briefings={current}, chat={chat}', flush=True)
                        if not attempts and not versions and not any(current.values()) and not chat:
                            return
                        if time.monotonic() > deadline:
                            raise TimeoutError('AI/Chat drain exceeded one hour; running services were not stopped')
                        await asyncio.sleep(10)
            finally:
                if principal is not None:
                    await revoke_session(principal)
        else:
            raise ValueError('Unknown maintenance action')
    finally:
        await engine.dispose()


if __name__ == '__main__':
    action, config, journal, base = sys.argv[1:]
    os.environ.update({k: v for k, v in dotenv_values(config, interpolate=False).items() if v is not None})
    asyncio.run(execute(action, Path(journal), base))
