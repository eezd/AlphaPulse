#!/usr/bin/env python3
"""Update the one data-bearing dev instance using verified image digests."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

from ci import IMAGES, ROOT, branch_head, locked, require, run, save

INSTANCE = Path('/docker/gitea/dev/AlphaPulse')
SERVICES = ('api', 'web', 'market', 'crawler', 'calendar', 'trader')

SCHEMA = '''
import asyncio, json
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from alphapulse.core.db import engine
async def main():
    async with engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            actual = list((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert sorted(actual) == sorted(expected), "Database migration requires separate authorization"
    await engine.dispose()
    print(json.dumps({"schema": actual}))
asyncio.run(main())
'''

IDLE = '''
import asyncio, json
import httpx
from sqlalchemy import text
from alphapulse.core.auth import create_token, require_session, revoke_session
from fastapi.security import HTTPAuthorizationCredentials
from alphapulse.core.config import get_settings
from alphapulse.core.db import engine
async def main():
    async with engine.connect() as connection:
        busy = await connection.scalar(text("SELECT count(*) FROM trading_analyses WHERE status='running'"))
    token, _ = await create_token(get_settings().admin_username)
    principal = await require_session(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get("http://localhost:8000/api/chat/session", headers={"Authorization":"Bearer "+token})
            response.raise_for_status()
            busy += int(response.json()["data"]["generating"])
    finally:
        await revoke_session(principal)
        await engine.dispose()
    print(json.dumps({"busy": busy}))
asyncio.run(main())
'''


def validate_images(build):
    require(build.get('verified') is True, 'Build has not passed isolated acceptance')
    require(re.fullmatch('[0-9a-f]{40}', build['commit']), 'Invalid commit SHA')
    require(re.fullmatch(r'\d{8}-[0-9a-f]{8}', build['version']), 'Invalid development version')
    require(build['version'].endswith(build['commit'][:8]), 'Version does not identify this commit')
    for part, repository in IMAGES.items():
        image = build['images'][part]['digest']
        require(re.fullmatch(re.escape(repository) + r'@sha256:[0-9a-f]{64}', image), 'Expected a development image digest')
        run('docker', 'pull', image)
        info = json.loads(run('docker', 'image', 'inspect', image))[0]
        labels = info['Config'].get('Labels') or {}
        require(labels.get('org.opencontainers.image.revision') == build['commit'], 'Image revision mismatch')
        require(labels.get('org.opencontainers.image.version') == build['version'], 'Image version mismatch')
        require(info['Architecture'] == 'amd64' and info['Os'] == 'linux', 'Unexpected image architecture')


def compose(directory, release):
    images = directory / 'images.env'
    images.write_text(''.join(f'ALPHAPULSE_{part.upper()}_IMAGE={release["images"][part]["digest"]}\n' for part in IMAGES))
    return ['docker', 'compose', '--project-name', 'alphapulse-dev', '--env-file', str(images),
            '-f', str(directory / 'compose.yml')]


def inspect_services(command):
    identifiers = run(*command, 'ps', '-aq').split()
    return json.loads(run('docker', 'inspect', *identifiers)) if identifiers else []


def health(command, build):
    containers = inspect_services(command)
    require({c['Config']['Labels']['com.docker.compose.service'] for c in containers} == set(SERVICES), 'Missing services')
    for container in containers:
        require(container['State']['Running'] and container['RestartCount'] == 0, 'Service exited or restarted')
        part = 'web' if container['Name'] == '/AlphaPulse-dev' else 'backend'
        expected = json.loads(run('docker', 'image', 'inspect', build['images'][part]['digest']))[0]['Id']
        require(container['Image'] == expected, 'Running image differs from the accepted digest')
    payload = run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-c',
                  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://web/api/health"))["data"]["commit"])')
    require(payload == build['commit'], 'Served API revision mismatch')
    payload = run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-c',
                  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://web/version.json"))["commit"])')
    require(payload == build['commit'], 'Served frontend revision mismatch')


def deploy(build, directory=INSTANCE):
    directory = Path(directory).resolve(strict=True)
    require(directory == INSTANCE, 'Unexpected instance directory')
    with locked(directory / '.deploy.lock'):
        require(not (directory / 'deployment.pending.json').exists(), 'Resolve the previous pending deployment with recover first')
        if branch_head() != build['commit']:
            print('Skipped: dev has advanced')
            return
        validate_images(build)
        # Only this read-only schema guard receives the existing application environment.
        print(run('docker', 'run', '--rm', '-i', '--network', 'dev_default', '--env-file', directory / '.env',
                  build['images']['backend']['digest'], 'python', '-', input=SCHEMA), flush=True)
        previous_file = directory / 'deployment.json'
        previous = json.loads(previous_file.read_text()) if previous_file.exists() else None
        old_compose = (directory / 'compose.yml').read_text() if previous else None
        previous_running = []
        if previous:
            old_command = compose(directory, previous)
            previous_running = [c['Config']['Labels']['com.docker.compose.service'] for c in inspect_services(old_command) if c['State']['Running']]
            for _ in range(60):
                idle = json.loads(run('docker', 'exec', '-i', 'AlphaPulse-dev-api', 'python', '-', input=IDLE))
                if not idle['busy']:
                    break
                time.sleep(5)
            else:
                raise RuntimeError('Chat or trading analysis did not become idle within five minutes')
        # Recheck after pulls and waiting; only a current dev commit can mutate the instance.
        if branch_head() != build['commit']:
            print('Skipped: dev advanced while preparing deployment')
            return
        if previous and 'trader' in previous_running:
            run('docker', 'pause', 'AlphaPulse-dev-trader')
            try:
                idle = json.loads(run('docker', 'exec', '-i', 'AlphaPulse-dev-api', 'python', '-', input=IDLE))
                require(not idle['busy'], 'Analysis became active during preparation; retry after it completes')
            except BaseException:
                run('docker', 'unpause', 'AlphaPulse-dev-trader')
                raise
        pending = {'build': build, 'previous': previous, 'previous_compose': old_compose,
                   'previous_running': previous_running, 'started_at': time.time()}
        save(directory / 'deployment.pending.json', pending)
        try:
            if previous:
                # Docker unpauses a paused container before delivering the stop signal.
                run(*old_command, 'stop', 'trader')
                run(*old_command, 'stop', 'crawler', 'calendar', 'market', 'web', 'api')
            (directory / 'compose.yml').write_text((ROOT / 'deploy/docker-compose.yml').read_text())
            command = compose(directory, build)
            run(*command, 'config', '--quiet')
            run(*command, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', 'api', 'web')
            run(*command, 'up', '-d', '--no-build', '--pull', 'never', 'market', 'crawler', 'calendar', 'trader')
            time.sleep(5)
            health(command, build)
            if previous:
                save(directory / 'deployment.previous.json', previous)
                (directory / 'compose.previous.yml').write_text(old_compose)
            save(previous_file, build)
            (directory / 'deployment.pending.json').unlink()
        except BaseException:
            # Never roll back or reset business data. A pending marker blocks subsequent jobs.
            subprocess.run(['docker', 'compose', '--project-name', 'alphapulse-dev', '--env-file', str(directory / 'images.env'),
                            '-f', str(directory / 'compose.yml'), 'stop'], capture_output=True)
            if previous:
                restore(directory, pending)
            raise
        print('All six services now run the accepted development digests', flush=True)


def restore(directory, pending):
    previous = pending['previous']
    require(previous is not None, 'No previous deployment exists; inspect the pending first-start failure')
    validate_images(previous)
    (directory / 'compose.yml').write_text(pending['previous_compose'])
    command = compose(directory, previous)
    run(*command, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', *pending['previous_running'])
    if set(pending['previous_running']) == set(SERVICES):
        health(command, previous)
    save(directory / 'deployment.json', previous)
    (directory / 'deployment.pending.json').unlink()
    print('Restored previous images and configuration; business data was retained', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('update', 'recover', 'rollback'))
    parser.add_argument('--build', type=Path, default=ROOT / 'build.json')
    args = parser.parse_args()
    if args.command == 'update':
        deploy(json.loads(args.build.read_text()))
    else:
        with locked(INSTANCE / '.deploy.lock'):
            if args.command == 'recover':
                restore(INSTANCE, json.loads((INSTANCE / 'deployment.pending.json').read_text()))
            else:
                require(not (INSTANCE / 'deployment.pending.json').exists(), 'Recover the pending deployment first')
                previous = json.loads((INSTANCE / 'deployment.previous.json').read_text())
                pending = {'previous': previous, 'previous_compose': (INSTANCE / 'compose.previous.yml').read_text(),
                           'previous_running': list(SERVICES)}
                save(INSTANCE / 'deployment.pending.json', pending)
                run(*compose(INSTANCE, json.loads((INSTANCE / 'deployment.json').read_text())), 'stop')
                restore(INSTANCE, pending)


if __name__ == '__main__':
    main()
