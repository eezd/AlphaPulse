#!/usr/bin/env python3
"""Update the one data-bearing dev instance using verified image version tags."""

import argparse
import hashlib
import secrets
import json
from pathlib import Path
import re
import subprocess
import time

from ci import IMAGES, ROOT, branch_head, locked, require, run, save

SEARXNG_IMAGE = 'docker.io/searxng/searxng:2026.10.30-ffa88e3ba'
SEARXNG_DIGEST = 'sha256:f4aa660c54d062dedb268be84e821ea24e39f48c7a496af8dd02cc9eedb93847'

INSTANCE = Path('/docker/gitea/dev/AlphaPulse')
SERVICES = ('api', 'web', 'market', 'crawler', 'calendar', 'trader', 'news-risk', 'jin10-calendar', 'searxng')

# Recognize the unmodified skill templates installed before entrypoint versioning.
LEGACY_ENTRIES = {
    'start.sh': 'b4709f36a66b428671a09f373f640a99259845924779dcb122f5cd9c17e85ab6',
    'stop.sh': '83348a7781b7d738a715bde51387951041a5cfcd31796805422ef83139a21571',
}


def install_entrypoints(directory):
    manifest = directory / 'entrypoints.json'
    previous = json.loads(manifest.read_text()) if manifest.exists() else LEGACY_ENTRIES
    sources = {name: (ROOT / 'deploy' / name).read_bytes() for name in ('instance.py', 'start.sh', 'stop.sh')}
    for name, content in sources.items():
        target = directory / name
        require(not target.is_symlink(), f'拒绝覆盖符号链接：{name}')
        require(not target.exists() or target.read_bytes() == content or
                hashlib.sha256(target.read_bytes()).hexdigest() == previous.get(name), f'保留用户自定义脚本，请先核对：{name}')
    for name, content in sources.items():
        target = directory / name
        temporary = target.with_name(name + '.tmp')
        temporary.write_bytes(content)
        temporary.chmod(0o755 if name.endswith('.sh') else 0o644)
        temporary.replace(target)
    save(manifest, {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()})

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
        digest = build['images'][part]['digest']
        require(re.fullmatch(re.escape(repository) + r'@sha256:[0-9a-f]{64}', digest), 'Invalid image identity in build receipt')
        image = repository + ':' + build['version']
        run('docker', 'pull', image)
        info = json.loads(run('docker', 'image', 'inspect', image))[0]
        require(digest in info.get('RepoDigests', []), 'Version tag differs from the accepted image')
        labels = info['Config'].get('Labels') or {}
        require(labels.get('org.opencontainers.image.revision') == build['commit'], 'Image revision mismatch')
        require(labels.get('org.opencontainers.image.version') == build['version'], 'Image version mismatch')
        require(info['Architecture'] == 'amd64' and info['Os'] == 'linux', 'Unexpected image architecture')


def validate_search_image():
    info = json.loads(run('docker', 'image', 'inspect', SEARXNG_IMAGE))[0]
    require(any(ref.rsplit('@', 1)[-1] == SEARXNG_DIGEST for ref in info.get('RepoDigests', [])),
            'Search version tag differs from the accepted image')
    return info


def compose(directory, release):
    images = directory / 'images.env'
    images.write_text(''.join(f'ALPHAPULSE_{part.upper()}_IMAGE={IMAGES[part]}:{release["version"]}\n' for part in IMAGES))
    return ['docker', 'compose', '--project-name', 'alphapulse-dev', '--env-file', str(images),
            '-f', str(directory / 'compose.yml')]


def inspect_services(command):
    identifiers = run(*command, 'ps', '-aq').split()
    return json.loads(run('docker', 'inspect', *identifiers)) if identifiers else []


def health(command, build, expected_services=SERVICES):
    containers = inspect_services(command)
    require({c['Config']['Labels']['com.docker.compose.service'] for c in containers} == set(expected_services), 'Missing services')
    for container in containers:
        require(container['State']['Running'] and not container['State']['Paused'] and container['RestartCount'] == 0,
                'Service exited, paused or restarted')
        service = container['Config']['Labels']['com.docker.compose.service']
        if service == 'searxng':
            expected = json.loads(run('docker', 'image', 'inspect', SEARXNG_IMAGE))[0]['Id']
            require(container['Image'] == expected, 'Unexpected search image')
            continue
        part = 'web' if container['Name'] == '/AlphaPulse-dev' else 'backend'
        expected = json.loads(run('docker', 'image', 'inspect', IMAGES[part] + ':' + build['version']))[0]['Id']
        require(container['Image'] == expected, 'Running image differs from the accepted digest')
    payload = run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-c',
                  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://web/api/health"))["data"]["commit"])')
    require(payload == build['commit'], 'Served API revision mismatch')
    payload = run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-c',
                  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://web/version.json"))["commit"])')
    require(payload == build['commit'], 'Served frontend revision mismatch')
    if 'searxng' in expected_services:
        payload = run('docker', 'exec', 'AlphaPulse-dev-api', 'python', '-c',
                      'import urllib.request; print(urllib.request.urlopen("http://searxng:8080/healthz", timeout=10).status)')
        require(payload == '200', 'Search service health check failed')


def deploy(build, directory=INSTANCE):
    directory = Path(directory).resolve(strict=True)
    require(directory == INSTANCE, 'Unexpected instance directory')
    with locked(directory / '.deploy.lock'):
        require(not (directory / 'deployment.pending.json').exists(), 'Resolve the previous pending deployment with recover first')
        if branch_head() != build['commit']:
            print('Skipped: dev has advanced')
            return
        validate_images(build)
        run('docker', 'pull', SEARXNG_IMAGE)
        validate_search_image()
        # Only this read-only schema guard receives the existing application environment.
        print(run('docker', 'run', '--rm', '-i', '--network', 'dev_default', '--env-file', directory / '.env',
                  IMAGES['backend'] + ':' + build['version'], 'python', '-', input=SCHEMA), flush=True)
        previous_file = directory / 'deployment.json'
        previous = json.loads(previous_file.read_text()) if previous_file.exists() else None
        old_compose = (directory / 'compose.yml').read_text() if previous else None
        previous_running = []
        if previous:
            old_command = compose(directory, previous)
            containers = inspect_services(old_command)
            require(not any(c['State'].get('Paused') for c in containers), 'Recover paused services before deploying')
            previous_running = [c['Config']['Labels']['com.docker.compose.service'] for c in containers if c['State']['Running']]
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
        install_entrypoints(directory)
        pending = {'build': build, 'previous': previous, 'previous_compose': old_compose,
                   'previous_running': previous_running, 'started_at': time.time(), 'phase': 'preparing'}
        save(directory / 'deployment.pending.json', pending)
        try:
            if previous and 'trader' in previous_running:
                run('docker', 'pause', 'AlphaPulse-dev-trader')
                idle = json.loads(run('docker', 'exec', '-i', 'AlphaPulse-dev-api', 'python', '-', input=IDLE))
                require(not idle['busy'], 'Analysis became active during preparation; retry after it completes')
            # Persist the transition before stopping any existing service.
            save(directory / 'deployment.pending.json', {**pending, 'phase': 'updating'})
            pending['phase'] = 'updating'
            if previous:
                # Docker unpauses a paused container before delivering the stop signal.
                run(*old_command, 'stop', 'trader')
                run(*old_command, 'stop', *[service for service in previous_running if service != 'trader'])
            (directory / 'compose.yml').write_text((ROOT / 'deploy/docker-compose.yml').read_text())
            search_config = directory / 'searxng.yml'
            if not search_config.exists():
                search_config.write_text((ROOT / 'deploy/searxng.yml').read_text().replace('@SEARXNG_SECRET@', secrets.token_hex(32)))
                search_config.chmod(0o644)
            command = compose(directory, build)
            run(*command, 'config', '--quiet')
            run(*command, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', 'api', 'web')
            run(*command, 'up', '-d', '--no-build', '--pull', 'never', 'market', 'crawler', 'calendar', 'trader', 'news-risk', 'jin10-calendar', 'searxng')
            time.sleep(5)
            health(command, build)
            if previous:
                save(directory / 'deployment.previous.json', previous)
                (directory / 'compose.previous.yml').write_text(old_compose)
            save(previous_file, build)
            (directory / 'deployment.pending.json').unlink()
        except BaseException:
            if pending['phase'] == 'preparing':
                restore(directory, pending)
                raise
            # Never roll back or reset business data. A pending marker blocks subsequent jobs.
            subprocess.run(['docker', 'compose', '--project-name', 'alphapulse-dev', '--env-file', str(directory / 'images.env'),
                            '-f', str(directory / 'compose.yml'), 'stop'], capture_output=True)
            if previous:
                restore(directory, pending)
            raise
        print('All application services now run the accepted development version tags', flush=True)


def restore(directory, pending):
    previous = pending['previous']
    require(previous is not None, 'No previous deployment exists; inspect the pending first-start failure')
    trader = run('docker', 'ps', '-aq', '--filter', 'name=^/AlphaPulse-dev-trader$')
    if trader:
        state = json.loads(run('docker', 'inspect', trader))[0]['State']
        if state['Paused']:
            run('docker', 'unpause', trader)
    if pending.get('phase') == 'preparing':
        # No service has been stopped or replaced during preparation.
        (directory / 'deployment.pending.json').unlink()
        print('Restored preparation state; existing services were retained', flush=True)
        return
    validate_images(previous)
    previous_compose = pending['previous_compose'].replace('docker.io/searxng/searxng@' + SEARXNG_DIGEST, SEARXNG_IMAGE)
    require(not re.search(r'^\s*image:.*@sha256:', previous_compose, re.M),
            'Previous Compose contains an unknown digest reference; verify its version tag before recovery')
    if re.search(r'^  searxng:$', previous_compose, re.M):
        validate_search_image()
    (directory / 'compose.yml').write_text(previous_compose)
    command = compose(directory, previous)
    run(*command, 'up', '-d', '--no-build', '--pull', 'never', '--remove-orphans', '--wait', '--wait-timeout', '120', *pending['previous_running'])
    previous_services = re.findall(r'^  ([a-z][a-z0-9-]*):$', pending['previous_compose'], re.M)
    if set(pending['previous_running']) == set(previous_services):
        health(command, previous, previous_services)
    save(directory / 'deployment.json', previous)
    (directory / 'deployment.pending.json').unlink()
    print('Restored previous images and configuration; business data was retained', flush=True)



def reset_news_ai(build, confirmed=False):
    """Preview or clear only legacy news AI records under the deployment lock."""
    validate_images(build)
    with locked(INSTANCE / '.deploy.lock'):
        require(not (INSTANCE / 'deployment.pending.json').exists(), 'Recover the pending deployment first')
        command = ['docker', 'run', '--rm', '-i', '--network', 'dev_default', '--env-file', str(INSTANCE / '.env'),
                   IMAGES['backend'] + ':' + build['version'], 'python', '-']
        run(*command, input=SCHEMA)
        if confirmed:
            current = json.loads((INSTANCE / 'deployment.json').read_text())
            run(*compose(INSTANCE, current), 'stop', 'news-risk')
        program = """
import asyncio,json
from sqlalchemy import text
from alphapulse.core.db import engine
async def main():
    async with engine.begin() as c:
        if not CONFIRMED:
            await c.execute(text('SET TRANSACTION READ ONLY'))
        counts = {table: await c.scalar(text('SELECT count(*) FROM '+table)) for table in
            ('risk_analyses','risk_events','risk_snapshots')}
        if CONFIRMED:
            assert not await c.scalar(text('SELECT count(*) FROM news_ai_article_analyses')), 'New pipeline already contains data'
            assert not await c.scalar(text('SELECT count(*) FROM news_ai_summaries')), 'New pipeline already contains data'
            await c.execute(text('UPDATE risk_settings SET enabled=false, start_at=NULL'))
            for table in ('risk_analyses','risk_events','risk_snapshots'):
                await c.execute(text('DELETE FROM '+table))
        print(json.dumps({'legacy_counts':counts,'cleared':CONFIRMED}))
    await engine.dispose()
asyncio.run(main())
"""
        print(run(*command, input='CONFIRMED = '+repr(confirmed)+'\n'+program), flush=True)

def reset_news_derived(build, execute=False, acceptance=None, baseline=None):
    """Run the scoped reset with both deployment and model-admission locks."""
    report = json.loads(acceptance.read_text()) if acceptance else None
    raw = baseline.read_bytes() if baseline else None
    if execute:
        require(bool(report and raw and report.get('real_models') is True and report.get('quality_accepted') is True
            and report.get('baseline_sha256') == hashlib.sha256(raw).hexdigest()
            and report.get('baseline_packages') == 20 and report.get('originals') == 27
            and report.get('failed') == 0 and report.get('unassigned') == 0), '固定样本真实模型质量验收尚未通过')
    validate_images(build)
    with locked(INSTANCE / '.deploy.lock'):
        require(not (INSTANCE / 'deployment.pending.json').exists(), 'Recover the pending deployment first')
        command = ['docker','run','--rm','-i','--network','dev_default','--env-file',str(INSTANCE / '.env'),
            IMAGES['backend']+':'+build['version'],'python','-']
        run(*command,input=SCHEMA)
        source = (ROOT / 'scripts/reset_news_ai.py').read_text()
        program = "import asyncio,json\nnamespace={'__name__':'news_reset'}\nexec("+repr(source)+",namespace)\n"
        program += 'print(json.dumps(asyncio.run(namespace["reset"]('+repr(execute)+','+repr(report)+','+repr(raw)+')),ensure_ascii=False))'
        print(run(*command,input=program),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('update', 'recover', 'rollback', 'reset-news-ai', 'reset-news-derived', 'install-entrypoints'))
    parser.add_argument('--build', type=Path, default=ROOT / 'build.json')
    parser.add_argument('--confirm-delete-old-news-ai', action='store_true')
    parser.add_argument('--confirm-reset-news-derived', action='store_true')
    parser.add_argument('--acceptance', type=Path)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    if args.command == 'install-entrypoints':
        with locked(INSTANCE / '.deploy.lock'):
            require(not (INSTANCE / 'deployment.pending.json').exists(), 'Recover the pending deployment first')
            install_entrypoints(INSTANCE)
        print('实例启停入口已安装；未启动或停止任何服务')
    elif args.command == 'reset-news-derived':
        reset_news_derived(json.loads(args.build.read_text()),args.confirm_reset_news_derived,args.acceptance,args.baseline)
    elif args.command == 'reset-news-ai':
        reset_news_ai(json.loads(args.build.read_text()), args.confirm_delete_old_news_ai)
    elif args.command == 'update':
        deploy(json.loads(args.build.read_text()))
    else:
        with locked(INSTANCE / '.deploy.lock'):
            if args.command == 'recover':
                restore(INSTANCE, json.loads((INSTANCE / 'deployment.pending.json').read_text()))
            else:
                require(not (INSTANCE / 'deployment.pending.json').exists(), 'Recover the pending deployment first')
                previous = json.loads((INSTANCE / 'deployment.previous.json').read_text())
                pending = {'previous': previous, 'previous_compose': (INSTANCE / 'compose.previous.yml').read_text(),
                           'previous_running': [name for name in re.findall(r'^  ([a-z][a-z0-9-]*):$',
                               (INSTANCE / 'compose.previous.yml').read_text(), re.M) if name in SERVICES]}
                save(INSTANCE / 'deployment.pending.json', pending)
                run(*compose(INSTANCE, json.loads((INSTANCE / 'deployment.json').read_text())), 'stop')
                restore(INSTANCE, pending)


if __name__ == '__main__':
    main()
