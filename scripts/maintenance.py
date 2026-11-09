"""Schema guard and bounded AI drain; never validate or call a model provider."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from dotenv import dotenv_values


def fingerprint(row):
    value = {'config': row.config, 'start_at': str(row.start_at), 'realtime_from': str(row.realtime_from)}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.next')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


async def execute(action, journal, base):
    from sqlalchemy import func, select, text
    from alphapulse.core.db import SessionLocal, engine
    from alphapulse.news_research.models import ResearchSettings, Attempt, Version
    try:
        if action == 'schema':
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            expected = set(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
            async with engine.connect() as connection:
                actual = set((await connection.execute(text('SELECT version_num FROM alembic_version'))).scalars())
            if actual != expected:
                raise RuntimeError(f'Schema mismatch: database={actual}, candidate={expected}; migration requires separate authorization')
            print('Schema verified:', ','.join(sorted(actual)))
            return
        state = json.loads(journal.read_text())
        if action == 'pause':
            async with SessionLocal() as session:
                row = await session.get(ResearchSettings, 1, with_for_update=True)
                if row:
                    state['ai'] = {'enabled': row.enabled, 'revision': row.control_revision + 1, 'fingerprint': fingerprint(row)}
                    # Journal precedes the DB commit, so an interrupted pause is recoverable.
                    save(journal, state)
                    row.enabled = False
                    row.control_revision += 1
                    await session.commit()
            print('AI admissions paused; existing calls may finish', flush=True)
        elif action == 'assert-paused':
            async with SessionLocal() as session:
                row = await session.get(ResearchSettings, 1)
                previous = state.get('ai')
                if previous:
                    if not (row and not row.enabled and row.control_revision == previous['revision']
                            and fingerprint(row) == previous['fingerprint']):
                        raise RuntimeError('AI controls changed during maintenance; aborting before service stop')
                elif row:
                    raise RuntimeError('AI configuration was created during maintenance; aborting service stop')
        elif action == 'restore':
            previous = state.get('ai')
            if previous:
                async with SessionLocal() as session:
                    row = await session.get(ResearchSettings, 1, with_for_update=True)
                    if (row and not row.enabled and row.control_revision == previous['revision']
                            and fingerprint(row) == previous['fingerprint']):
                        row.enabled = previous['enabled']
                        row.control_revision += 1
                        await session.commit()
                        print('AI enabled state restored:', previous['enabled'])
                    else:
                        print('AI controls changed during maintenance; preserving newer user setting')
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
                            attempts = await session.scalar(select(func.count()).select_from(Attempt).where(Attempt.state == 'processing'))
                            versions = await session.scalar(select(func.count()).select_from(Version).where(Version.state == 'processing'))
                        chat = False
                        if principal is not None:
                            response = await client.get(base + '/api/chat/session', headers={'Authorization': 'Bearer ' + token})
                            response.raise_for_status()
                            chat = response.json()['data']['generating']
                        print(f'Draining: attempts={attempts}, event_versions={versions}, chat={chat}', flush=True)
                        if not attempts and not versions and not chat:
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
